"""Audited PAPER strategy stand-down from actual, sealed closed-trade outcomes."""

from decimal import Decimal
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, model_validator

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.db.models.journal import JournalEntry, JournalRevision
from app.modes import TradingMode
from app.notifications.outbox import enqueue
from app.portfolio.journal_integrity import journal_verified
from app.portfolio.metrics import closed_trade_drawdown_amount, performance
from app.strategies.base import StrategySpec
from app.strategies.paper_evidence import timestamp
from app.strategies.paper_policy import evidence_chain, registration
from app.strategies.registry import specification_hash


class DegradationPolicy(EvidenceModel):
    version: int = Field(gt=0, strict=True)
    window_trades: int = Field(ge=1, le=1000, strict=True)
    minimum_trades: int = Field(ge=1, le=1000, strict=True)
    maximum_drawdown_amount: Decimal = Field(gt=0)
    data_origin: DataOrigin

    @model_validator(mode="after")
    def coherent(self):
        if self.minimum_trades > self.window_trades:
            raise ValueError("minimum trades exceeds rolling window")
        return self


class DegradationView(EvidenceModel):
    strategy_id: str
    version: str
    as_of: AwareDatetime
    status: Literal["UNCONFIGURED", "WARMING_UP", "HEALTHY", "BREACHED", "UNAVAILABLE"]
    policy: DegradationPolicy | None = None
    policy_event_id: str | None = None
    last_event_id: str | None = None
    auto_disabled: bool
    disable_reason: str | None = None
    journal_ids: tuple[str, ...] = ()
    net_pnl: Decimal | None = None
    maximum_drawdown_amount: Decimal | None = None
    expectancy: Decimal | None = None
    win_rate: Decimal | None = None
    blockers: tuple[str, ...] = ()
    basis: str = "PAPER_CLOSED_TRADE_WINDOW_NET; NOT_PORTFOLIO_MTM_OR_LIVE_APPROVAL"


async def events_for(session, prefix, row, as_of):
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == evidence_chain(prefix, row.id),
                    AuditEvent.occurred_at <= as_of,
                )
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(events):
        raise ValueError("strategy degradation audit integrity failure")
    return events


async def publish_policy(session, row, policy, *, actor, reason, clock=None):
    clock = clock or get_clock()
    events = await events_for(session, "dgp", row, clock.utcnow())
    if policy.version != len(events) + 1:
        raise ValueError("next sequential degradation policy version required")
    return await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=evidence_chain("dgp", row.id),
            event_type="STRATEGY_DEGRADATION_POLICY",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "strategy_id": row.strategy_id,
            "result": {
                "parameter_hash": row.parameter_hash,
                "policy": policy.model_dump(mode="json"),
                "reason": reason,
            },
        },
        expected_count=len(events),
    )


async def trade_values(session, journals, row, policy, as_of):
    pnls, holding = [], []
    for journal in journals:
        seals = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.chain_id == journal.id,
                    )
                    .order_by(AuditEvent.sequence)
                )
            ).all()
        )
        if (
            not seals
            or _utc(seals[0].occurred_at) > as_of
            or not await journal_verified(session, journal.id)
            or journal.version != 1
            or journal.superseded_by is not None
            or await session.scalar(
                sa.select(JournalRevision.entry_id)
                .where(JournalRevision.root_id == journal.id)
                .limit(1)
            )
        ):
            raise ValueError("JOURNAL_INTEGRITY_OR_REVISION_REVIEW_REQUIRED")
        context = (journal.indicator_snapshot or {}).get("decision_context", {})
        if (
            specification_hash(StrategySpec.model_validate(context["strategy"]))
            != row.parameter_hash
            or context["market"]["data_origin"] != policy.data_origin.value
        ):
            raise ValueError("STRATEGY_OR_DATA_ORIGIN_MISMATCH")
        if (
            journal.indicator_snapshot.get("cost_status") != "ESTIMATED"
            or any(
                value is None or not value.is_finite()
                for value in (journal.net_pnl, journal.gross_pnl, journal.charges)
            )
            or journal.charges < 0
            or journal.gross_pnl - journal.charges != journal.net_pnl
        ):
            raise ValueError("NET_COST_ACCOUNTING_UNAVAILABLE")
        if not journal.opened_at or not _utc(journal.opened_at) <= _utc(journal.closed_at) <= as_of:
            raise ValueError("INVALID_TRADE_TIMELINE")
        market = context["market"]
        if (
            not timestamp(market["observed_at"])
            <= timestamp(market["available_at"])
            <= _utc(journal.opened_at)
        ):
            raise ValueError("FUTURE_MARKET_EVIDENCE")
        pnls.append(journal.net_pnl)
        holding.append(
            Decimal(str((_utc(journal.closed_at) - _utc(journal.opened_at)).total_seconds()))
        )
    return pnls, holding


async def review(session, row, *, clock=None):
    clock = clock or get_clock()
    as_of = clock.utcnow()
    policies = await events_for(session, "dgp", row, as_of)
    actions = await events_for(session, "dgm", row, as_of)
    base = {
        "strategy_id": row.strategy_id,
        "version": row.version,
        "as_of": as_of,
        "auto_disabled": row.auto_disabled,
        "disable_reason": row.auto_disabled_reason,
        "last_event_id": actions[-1].id if actions else None,
    }
    if not policies:
        return DegradationView(**base, status="UNCONFIGURED", blockers=("POLICY_UNAVAILABLE",))
    latest = policies[-1]
    if latest.result["parameter_hash"] != row.parameter_hash:
        raise ValueError("degradation policy parameter mismatch")
    policy = DegradationPolicy.model_validate(latest.result["policy"])
    base.update(policy=policy, policy_event_id=latest.id)
    journals = list(
        (
            await session.scalars(
                sa.select(JournalEntry)
                .where(
                    JournalEntry.strategy_id == row.strategy_id,
                    JournalEntry.strategy_version == row.version,
                    JournalEntry.mode == TradingMode.PAPER,
                    JournalEntry.kind == "TRADE",
                    JournalEntry.closed_at <= as_of,
                    sa.or_(
                        ~sa.exists(
                            sa.select(AuditEvent.id).where(
                                AuditEvent.chain_id == JournalEntry.id,
                                AuditEvent.sequence == 1,
                            )
                        ),
                        sa.exists(
                            sa.select(AuditEvent.id).where(
                                AuditEvent.chain_id == JournalEntry.id,
                                AuditEvent.sequence == 1,
                                AuditEvent.occurred_at <= as_of,
                            )
                        ),
                    ),
                )
                .order_by(JournalEntry.closed_at.desc(), JournalEntry.id.desc())
                .limit(policy.window_trades)
            )
        ).all()
    )
    journals.reverse()
    base["journal_ids"] = tuple(journal.id for journal in journals)
    try:
        pnls, holding = await trade_values(session, journals, row, policy, as_of)
    except (ValueError, KeyError, TypeError) as error:
        return DegradationView(
            **base,
            status="UNAVAILABLE",
            blockers=(str(error) if type(error) is ValueError else "TRADE_EVIDENCE_UNAVAILABLE",),
        )
    metrics = performance([], pnls, holding)
    drawdown = closed_trade_drawdown_amount(pnls)
    status = (
        "WARMING_UP"
        if len(pnls) < policy.minimum_trades
        else ("BREACHED" if drawdown >= policy.maximum_drawdown_amount else "HEALTHY")
    )
    return DegradationView(
        **base,
        status=status,
        net_pnl=sum(pnls, Decimal(0)) if pnls else None,
        maximum_drawdown_amount=drawdown,
        expectancy=metrics["expectancy"],
        win_rate=metrics["win_rate"],
    )


async def enforce(strategy_id, version, *, clock=None):
    clock = clock or get_clock()
    async with db_session.session_scope() as session:
        row = await registration(session, strategy_id, version)
        result = await review(session, row, clock=clock)
        if (
            row.auto_disabled
            or not row.enabled_paper
            or result.status not in {"BREACHED", "UNAVAILABLE"}
        ):
            return result
        reason = "PAPER_DEGRADATION_" + result.status
        changed = await session.execute(
            sa.update(StrategyRegistration)
            .where(
                StrategyRegistration.id == row.id,
                StrategyRegistration.auto_disabled.is_(False),
            )
            .values(
                auto_disabled=True,
                auto_disabled_reason=reason,
                auto_disabled_at=clock.utcnow(),
                enabled_paper=False,
            )
        )
        if changed.rowcount != 1:
            raise ValueError("concurrent strategy degradation update")
        actions = await events_for(session, "dgm", row, clock.utcnow())
        event = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=evidence_chain("dgm", row.id),
                event_type="STRATEGY_AUTO_DISABLED",
                actor="strategy_monitor",
                mode=TradingMode.PAPER,
                severity="CRITICAL",
            ),
            {
                "strategy_id": strategy_id,
                "result": {"reason": reason, "evaluation": result.model_dump(mode="json")},
            },
            expected_count=len(actions),
        )
        await enqueue(
            session,
            key=event.id,
            event_type="STRATEGY_AUTO_DISABLED",
            severity="CRITICAL",
            message=f"PAPER strategy {strategy_id}/{version} disabled: {reason}",
            clock=clock,
            source_event=event,
        )
        return result.model_copy(
            update={"auto_disabled": True, "disable_reason": reason, "last_event_id": event.id}
        )


async def monitor_all(*, clock=None):
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    sa.select(StrategyRegistration).where(
                        StrategyRegistration.enabled_paper.is_(True),
                        StrategyRegistration.auto_disabled.is_(False),
                    )
                )
            ).all()
        )
    for row in rows:
        await enforce(row.strategy_id, row.version, clock=clock)


async def reset(session, row, *, policy_event_id, last_event_id, actor, reason, clock=None):
    clock = clock or get_clock()
    result = await review(session, row, clock=clock)
    if (
        result.status != "HEALTHY"
        or not row.auto_disabled
        or not (row.auto_disabled_reason or "").startswith("PAPER_DEGRADATION_")
        or result.policy_event_id != policy_event_id
        or result.last_event_id != last_event_id
    ):
        raise ValueError("healthy evidence and current disable/policy review required")
    row.auto_disabled = False
    row.auto_disabled_reason = row.auto_disabled_at = None
    row.enabled_paper = False
    actions = await events_for(session, "dgm", row, clock.utcnow())
    if not actions or actions[-1].id != last_event_id:
        raise ValueError("strategy disable state changed during review")
    return await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=evidence_chain("dgm", row.id),
            event_type="STRATEGY_DEGRADATION_RESET",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "strategy_id": row.strategy_id,
            "result": {
                "reason": reason,
                "evaluation": result.model_dump(mode="json"),
                "enabled_paper": False,
            },
        },
        expected_count=len(actions),
    )
