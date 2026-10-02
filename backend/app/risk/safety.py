"""Durable entry latches in the transactional audit ledger; no reset authority here."""

import hashlib
import logging
from datetime import date, timedelta
from decimal import Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Severity
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.event_outbox import RuntimeEventOutbox
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.notifications.outbox import enqueue
from app.notifications.runtime import emit_critical
from app.risk.active import active_limits
from app.risk.budgets import daily_loss_limit, drawdown_limit
from app.risk.config import RiskLimits
from app.risk.emergency_state import require_no_emergency
from app.risk.event_controls import require_event_entries
from app.risk.instrument_blocks import require_instrument_entries
from app.risk.models import MarketState, PortfolioState
from app.risk.news_halts import require_no_news_halt

logger = logging.getLogger(__name__)


def new_latches(before, after):
    return tuple(
        (field, event_type)
        for field, event_type in (
            ("daily_loss", "DAILY_LOSS_LIMIT"),
            ("drawdown", "DRAWDOWN_LIMIT"),
            ("engine_error", "RISK_ENGINE_ERROR"),
        )
        if getattr(after, field)
        and (
            not getattr(before, field)
            or (field == "daily_loss" and before.session_date != after.session_date)
        )
    )


class SafetyState(EvidenceModel):
    session_date: date | None = None
    last_observed: AwareDatetime | None = None
    daily_loss: bool = False
    drawdown: bool = False
    engine_error: bool = False

    @property
    def code(self):
        if self.engine_error:
            return "RISK_ERROR_LATCHED"
        if self.drawdown:
            return "DRAWDOWN_LATCHED"
        if self.daily_loss:
            return "DAILY_LOSS_LATCHED"
        return None


class RiskSafety:
    def __init__(self, *, clock=None, gate=None):
        self.clock = clock or get_clock()
        self.gate = gate or get_trading_gate()

    @staticmethod
    def chain_id(mode, origin):
        identity = f"{TradingMode(mode).value}:{DataOrigin(origin).value}"
        return "risk-" + hashlib.sha256(identity.encode()).hexdigest()[:35]

    async def _load(self, session, chain_id):
        records = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == chain_id)
                    .order_by(AuditEvent.sequence)
                    .with_for_update()
                )
            ).all()
        )
        if not verify_records(records):
            raise ValueError("risk safety ledger integrity failure")
        state = (
            SafetyState.model_validate(records[-1].result["state"]) if records else SafetyState()
        )
        return state, len(records)

    def _publish(self, chain_id, state):
        if state.code:
            self.gate.block(chain_id, state.code)
        else:
            self.gate.clear(chain_id)

    def _failed(self):
        self.gate.block("risk_storage", "RISK_SAFETY_UNAVAILABLE")
        logger.critical("RISK_SAFETY_UNAVAILABLE: entries disabled; recovery requires review")
        emit_critical(
            event_id=new_id("err"),
            event_type="RISK_SAFETY_UNAVAILABLE",
            message="Risk safety storage/evidence failed. Entries remain blocked; review required.",
            condition_key="risk_storage",
            occurred_at=self.clock.now(),
        )

    async def _notify_changes(self, session, before, after, record, *, mode, origin):
        for field, event_type in new_latches(before, after):
            await enqueue(
                session,
                key=f"risk:{record.id}:{field}",
                event_type=event_type,
                severity="CRITICAL",
                message=(
                    f"{event_type}: new entries blocked. Mode={mode}, data_origin={origin}; "
                    f"audit={record.id}."
                ),
                clock=self.clock,
                source_event=record,
            )

    async def restore(self, mode, origin):
        chain_id = self.chain_id(mode, origin)
        try:
            async with db_session.session_scope() as session:
                state, _ = await self._load(session, chain_id)
            self._publish(chain_id, state)
            return state
        except Exception:
            self._failed()
            raise

    async def require_entries_in_session(self, session, market, limits, *, strategy_id=None):
        await require_no_emergency(session)
        await require_no_news_halt(session, market)
        await require_instrument_entries(session, market)
        event_control = await require_event_entries(session, market, strategy_id=strategy_id)
        configured = await active_limits(session)
        if configured is not None and configured != limits:
            raise ValueError("risk configuration changed before approval")
        chain_id = self.chain_id(market.mode, market.data_origin)
        state, count = await self._load(session, chain_id)
        if not count or state.code:
            raise ValueError("durable risk latch blocks entry approval")
        if (
            not timedelta(0)
            <= self.clock.now() - market.as_of
            <= timedelta(
                seconds=min(limits.max_portfolio_age_seconds, limits.max_market_age_seconds)
            )
        ):
            raise ValueError("decision became stale before approval")
        self.gate.require_new_entries_allowed()
        return event_control

    async def _record(self, session, mode, origin, *, before, after, evidence, expected_count):
        critical = bool(new_latches(before, after))
        record = await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=self.chain_id(mode, origin),
                event_type="RISK_SAFETY",
                actor="risk_safety",
                mode=mode,
                severity=Severity.CRITICAL if critical else Severity.INFO,
            ),
            {
                "data_used": evidence,
                "result": {
                    "before": before.model_dump(mode="json"),
                    "state": after.model_dump(mode="json"),
                    "reason_code": after.code,
                },
            },
            expected_count=expected_count,
        )
        await self._notify_changes(session, before, after, record, mode=mode, origin=origin)
        if TradingMode(mode) == TradingMode.PAPER and any(
            getattr(before, field) != getattr(after, field)
            for field in ("daily_loss", "drawdown", "engine_error")
        ):
            session.add(RuntimeEventOutbox(audit_id=record.id))
        return critical, record.id

    async def observe(self, portfolio, market, limits):
        chain_id = self.chain_id(market.mode, market.data_origin)
        try:
            portfolio = PortfolioState.model_validate(portfolio.model_dump())
            market = MarketState.model_validate(market.model_dump())
            limits = RiskLimits.model_validate(limits.model_dump())
            age = timedelta(seconds=limits.max_portfolio_age_seconds)
            if (
                portfolio.data_origin != market.data_origin
                or not portfolio.observed_at <= portfolio.available_at <= market.as_of
                or not timedelta(0) <= self.clock.now() - market.as_of <= age
                or market.as_of - portfolio.observed_at > age
                or portfolio.observed_at.astimezone(IST).date()
                != market.as_of.astimezone(IST).date()
            ):
                raise ValueError("stale or future risk safety evidence")
            async with db_session.session_scope() as session:
                before, count = await self._load(session, chain_id)
                if before.last_observed and portfolio.observed_at < before.last_observed:
                    raise ValueError("risk safety evidence moved backwards")
                session_date = market.as_of.astimezone(IST).date()
                if before.session_date and session_date < before.session_date:
                    raise ValueError("risk safety session moved backwards")
                loss = max(Decimal(0), -portfolio.realised_day_pnl - portfolio.unrealised_day_pnl)
                daily_limit = daily_loss_limit(portfolio.equity, limits)
                maximum_drawdown = drawdown_limit(portfolio.peak_equity, limits)
                after = SafetyState(
                    session_date=session_date,
                    last_observed=portfolio.observed_at,
                    daily_loss=(before.daily_loss and before.session_date == session_date)
                    or loss >= daily_limit
                    or portfolio.entries_blocked,
                    drawdown=before.drawdown
                    or portfolio.drawdown_disarmed
                    or portfolio.peak_equity - portfolio.equity >= maximum_drawdown,
                    engine_error=before.engine_error,
                )
                critical, _event_id = await self._record(
                    session,
                    market.mode,
                    market.data_origin,
                    before=before,
                    after=after,
                    evidence={
                        "portfolio": portfolio.model_dump(mode="json"),
                        "limits": limits.model_dump(mode="json"),
                        "as_of": market.as_of,
                    },
                    expected_count=count,
                )
            self._publish(chain_id, after)
            if critical:
                logger.critical("Risk safety latch engaged: %s", after.code)
            return after
        except Exception:
            self._failed()
            raise

    async def trip_error(self, mode, origin):
        chain_id = self.chain_id(mode, origin)
        self.gate.block(chain_id, "RISK_ERROR_LATCHED")
        logger.critical("Risk engine failure: entries disabled")
        try:
            async with db_session.session_scope() as session:
                before, count = await self._load(session, chain_id)
                after = before.model_copy(update={"engine_error": True})
                await self._record(
                    session,
                    mode,
                    origin,
                    before=before,
                    after=after,
                    evidence={"error": "RISK_ENGINE_ERROR"},
                    expected_count=count,
                )
            return after
        except Exception:
            self._failed()
            raise
