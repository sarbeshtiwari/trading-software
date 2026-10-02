"""Opt-in observations of actual PAPER worker coverage; no inferred session history."""

from dataclasses import asdict
from datetime import timedelta

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST
from app.core.data_origin import DataOrigin
from app.core.enums import OrderStatus
from app.core.ids import new_id
from app.core.sessions import session_bounds
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.db.models.instrument import Instrument
from app.db.models.trading import Order, Position
from app.marketdata.models import InstrumentRef
from app.modes import TradingMode
from app.strategies.base import StrategySpec
from app.strategies.paper_policy import current_policy, evidence_chain
from app.strategies.reference import REFERENCE_IDS
from app.strategies.registry import specification_hash


async def observed_quote(executor, instrument):
    try:
        quote = await executor.quote(
            InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
        )
        if (
            quote is None
            or quote.data_origin != DataOrigin.LIVE
            or not quote.bids
            or not quote.asks
        ):
            return quote, "LIVE_DEPTH_UNAVAILABLE"
        return quote, None
    except Exception as error:
        return None, type(error).__name__


class PaperCoverage:
    def __init__(self, worker):
        self.worker = worker
        self.owner = new_id("pwr")

    async def sample(self):
        worker, executor = self.worker, self.worker.executor
        now = worker.clock.now().astimezone(IST)
        if not worker.calendar.is_year_complete(now.year):
            return
        bounds = session_bounds(now.date(), calendar=worker.calendar)
        if bounds is None or not bounds[0] <= now <= bounds[1] + timedelta(minutes=10):
            return
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(StrategyRegistration).where(
                            StrategyRegistration.strategy_id.in_(REFERENCE_IDS),
                            StrategyRegistration.version == "1",
                            StrategyRegistration.enabled_paper.is_(True),
                            StrategyRegistration.auto_disabled.is_(False),
                        )
                    )
                ).all()
            )
            for row in rows:
                policy, policy_event = await current_policy(session, row)
                if policy is None:
                    continue
                spec = StrategySpec.model_validate(row.parameters)
                if specification_hash(spec) != row.parameter_hash:
                    raise ValueError("coverage strategy parameter mismatch")
                chain_id = evidence_chain("pec", f"{row.id}:{policy_event.id}:{now.date()}")
                previous = await session.scalar(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == chain_id)
                    .order_by(AuditEvent.sequence.desc())
                    .limit(1)
                )
                if previous and previous.result["owner"] == self.owner:
                    elapsed = (now - _utc(previous.occurred_at)).total_seconds()
                    if elapsed < 0:
                        raise ValueError("coverage clock regressed")
                    if elapsed < policy.sample_interval_seconds:
                        continue
                instruments = list((await session.scalars(sa.select(Instrument))).all())
                selected = [
                    item
                    for item in instruments
                    if item.key in spec.universe
                    or f"{item.exchange.value}:{item.trading_symbol}" in spec.universe
                ]
                provider = worker.reference_runtime.provider if worker.reference_runtime else None
                origin = provider.data_origin if provider else None
                healthy = executor.ready and not worker.failed and executor.gate.trading_enabled
                reason, quote = None, None
                if len(selected) != 1 or origin != DataOrigin.LIVE:
                    healthy, reason = False, "LIVE_SOURCE_AND_RESOLVED_INSTRUMENT_REQUIRED"
                elif now < bounds[1]:
                    quote, reason = await observed_quote(executor, selected[0])
                    healthy = healthy and reason is None
                open_positions = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Position)
                    .where(Position.mode == TradingMode.PAPER, Position.net_quantity != 0)
                )
                pending_orders = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Order)
                    .where(
                        Order.mode == TradingMode.PAPER,
                        ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
                    )
                )
                collected = worker.clock.now()
                if quote is not None and not timedelta(
                    0
                ) <= collected - quote.observed_at <= timedelta(
                    seconds=executor.settings.tick_staleness_seconds
                ):
                    healthy, reason = False, "QUOTE_EXPIRED_DURING_COLLECTION"
                ended = (
                    collected >= bounds[1] and healthy and not open_positions and not pending_orders
                )
                await AuditService(worker.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=chain_id,
                        event_type="PAPER_COVERAGE_SAMPLE",
                        actor="paper_worker",
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "strategy_id": row.strategy_id,
                        "result": {
                            "registration_id": row.id,
                            "parameter_hash": row.parameter_hash,
                            "policy_event_id": policy_event.id,
                            "owner": self.owner,
                            "session_date": now.date().isoformat(),
                            "session_start": bounds[0],
                            "session_end": bounds[1],
                            "calendar_source": worker.calendar.source,
                            "observed_at": collected,
                            "data_origin": origin,
                            "healthy": healthy,
                            "reason": reason,
                            "ended_flat": ended,
                            "quote": asdict(quote) if quote is not None else None,
                            "gate": executor.gate.state.to_dict(),
                        },
                    },
                    expected_count=previous.sequence if previous else 0,
                )
