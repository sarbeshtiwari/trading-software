"""Pinned reference exit policy over durable position state and real observations."""

import os
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.analysis.equity import PriceObservation
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC
from app.core.enums import ExitReason, Severity, SignalDirection
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.instrument import Instrument
from app.db.models.system import Heartbeat
from app.db.models.trading import Position
from app.marketdata.models import InstrumentRef
from app.modes import TradingMode
from app.notifications.outbox import enqueue
from app.strategies.base import StrategySpec
from app.strategies.context import build_strategy_context
from app.strategies.exits import ExitContext, ExitState
from app.strategies.reference import REFERENCE_IDS, ClosedCandleBreakout

REASONS = {
    "STOP": ExitReason.STOP_LOSS,
    "TARGET": ExitReason.TARGET,
    "TRAILING": ExitReason.TRAILING_STOP,
    "TIME": ExitReason.TIME_EXIT,
    "INVALIDATION": ExitReason.INVALIDATION,
}


class ReferenceExitMonitor:
    def __init__(self, executor, ingestion):
        self.executor, self.ingestion, self.clock = executor, ingestion, executor.clock

    async def cycle(self):
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER,
                            Position.net_quantity != 0,
                            Position.strategy_id.in_(REFERENCE_IDS),
                        )
                    )
                ).all()
            )
        for position in positions:
            try:
                await self._evaluate(position)
            except Exception as error:
                self.executor.gate.block("paper_reference_exit", "REFERENCE_EXIT_MONITOR_FAILED")
                async with db_session.session_scope() as session:
                    heartbeat = await session.get(Heartbeat, "paper-worker", with_for_update=True)
                    if heartbeat is None:
                        heartbeat = Heartbeat(id="paper-worker")
                        session.add(heartbeat)
                    heartbeat.beat_at = self.clock.utcnow()
                    heartbeat.process_id = str(os.getpid())
                    heartbeat.detail = {
                        "failed": True,
                        "phase": "REFERENCE_EXIT_FAILURE",
                        "detail": "REFERENCE_EXIT_MONITOR_FAILED",
                        "mode": "PAPER",
                        "execution_realism": "SIMULATED",
                    }
                    record = await AuditService(self.clock).append_in_session(
                        session,
                        AuditIdentity(
                            chain_id=new_id("exf"),
                            event_type="REFERENCE_EXIT_FAILURE",
                            actor="reference_exit_monitor",
                            mode=TradingMode.PAPER,
                            severity=Severity.CRITICAL,
                        ),
                        {
                            "position_id": position.id,
                            "proposal_id": position.proposal_id,
                            "result": {"error_type": type(error).__name__, "entries_blocked": True},
                        },
                    )
                    await enqueue(
                        session,
                        key="reference-exit-failure:" + position.id,
                        event_type="PAPER_EXIT_MONITOR_FAILURE",
                        severity=Severity.CRITICAL,
                        message=(
                            "PAPER reference exit monitoring failed; entries blocked. "
                            "Fixed stop/target monitoring still requires valid quotes."
                        ),
                        clock=self.clock,
                        source_event=record,
                    )
                raise
        self.executor.gate.clear("paper_reference_exit")

    async def _evaluate(self, position):
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, position.proposal_id)
            instrument = await session.get(Instrument, position.instrument_id)
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == position.id)
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )
        if proposal is None or instrument is None or not verify_records(records):
            raise SafetyError("REFERENCE_EXIT_LINEAGE_UNAVAILABLE")
        spec = StrategySpec.model_validate(proposal.context_snapshot["strategy"])
        strategy = ClosedCandleBreakout(
            instrument.id,
            spec.universe[0],
            instrument.tick_size,
            spec.risk.requested_risk_fraction,
            clock=self.clock,
            long_option=spec.id == "long-option-breakout",
        )
        if strategy.spec != spec:
            raise SafetyError("REFERENCE_EXIT_SPECIFICATION_CHANGED")
        state = self._state(position, records)
        observation, invalidation = None, None
        try:
            if self.ingestion is not None:
                observation = await self.ingestion.collect(instrument.id)
        except Exception:
            observation = None
        if observation is not None:
            quote = observation.quote
            context = build_strategy_context(("candles", "indicators"), observation.available)
            invalidation = strategy.invalidation(context)
        else:
            quote = await self.executor.quote(
                InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
            )
        if quote is None or not quote.bids or not quote.asks:
            raise SafetyError("REFERENCE_EXIT_QUOTE_UNAVAILABLE")
        if quote.data_origin.value != proposal.context_snapshot["market"]["data_origin"]:
            raise SafetyError("REFERENCE_EXIT_SOURCE_MISMATCH")
        if (
            quote.observed_at < state.opened_at
            and not any(record.event_type == "REFERENCE_EXIT_STATE" for record in records)
            and timedelta(0)
            <= self.clock.now() - quote.observed_at
            <= timedelta(seconds=spec.exit_policy.max_mark_age_seconds)
        ):
            async with db_session.session_scope() as session:
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=position.id,
                        event_type="REFERENCE_EXIT_WAITING_FOR_POST_ENTRY_MARK",
                        actor="reference_exit_monitor",
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "position_id": position.id,
                        "proposal_id": proposal.id,
                        "result": {
                            "quote_observed_at": quote.observed_at,
                            "opened_at": state.opened_at,
                            "fixed_protection_required": True,
                        },
                    },
                    expected_count=len(records),
                )
            return
        price = quote.best_bid if position.net_quantity > 0 else quote.best_ask
        decision = strategy.exit(
            ExitContext(
                mark=PriceObservation(
                    value=price, observed_at=quote.observed_at, available_at=self.clock.now()
                ),
                invalidation=invalidation,
                as_of=self.clock.now(),
            ),
            state,
        )
        async with db_session.session_scope() as session:
            current = await session.get(Position, position.id, with_for_update=True)
            if current.net_quantity != position.net_quantity:
                raise SafetyError("REFERENCE_EXIT_POSITION_CHANGED")
            current.trailing_stop_price = decision.trailing_stop
            await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=position.id,
                    event_type="REFERENCE_EXIT_STATE",
                    actor="reference_exit_monitor",
                    mode=TradingMode.PAPER,
                ),
                {
                    "position_id": position.id,
                    "proposal_id": proposal.id,
                    "correlation_id": observation.chain_id if observation is not None else None,
                    "data_used": {
                        "invalidation": invalidation.model_dump(mode="json")
                        if invalidation is not None
                        else None,
                        "closed_at": observation.available["candles"].observed_at
                        if observation is not None
                        else None,
                        "quote": {
                            "observed_at": quote.observed_at,
                            "data_origin": quote.data_origin,
                            "bid": quote.best_bid,
                            "ask": quote.best_ask,
                        },
                        "quantity": position.net_quantity,
                    },
                    "result": decision.model_dump(mode="json"),
                },
                expected_count=len(records),
            )
        if decision.reason is not None:
            await self.executor.exit(position.id, REASONS[decision.reason])
            await self.executor.refresh_account(proposal, quote)
        if not decision.invalidation_checked:
            raise SafetyError("REFERENCE_EXIT_INVALIDATION_UNAVAILABLE")

    def _state(self, position, records):
        opened = position.opened_at
        if opened is None:
            raise SafetyError("REFERENCE_EXIT_ENTRY_TIME_UNAVAILABLE")
        opened = opened.replace(tzinfo=UTC) if opened.tzinfo is None else opened
        previous = [record for record in records if record.event_type == "REFERENCE_EXIT_STATE"]
        direction = SignalDirection.LONG if position.net_quantity > 0 else SignalDirection.SHORT
        if previous:
            state = ExitState.model_validate(previous[-1].result["state"])
            if (
                state.position_id != position.id
                or state.direction != direction
                or state.opened_at != opened
                or state.stop != position.stop_loss_price
                or state.target != position.target_price
                or position.trailing_stop_price != Decimal(previous[-1].result["trailing_stop"])
            ):
                raise SafetyError("REFERENCE_EXIT_STATE_CHANGED")
            return state
        if position.trailing_stop_price is not None:
            raise SafetyError("REFERENCE_EXIT_HISTORY_UNAVAILABLE")
        return ExitState(
            position_id=position.id,
            direction=direction,
            entry=position.average_price,
            stop=position.stop_loss_price,
            target=position.target_price,
            favourable_price=position.average_price,
            opened_at=opened,
            updated_at=opened,
        )
