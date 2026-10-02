"""Audited PAPER entry cancellation; protective exits are never swept by age."""

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order
from app.journal.order_actions import record_order_action
from app.modes import TradingMode


class PaperOrderHygiene:
    def __init__(self, executor):
        self.executor = executor
        self.clock = executor.clock

    async def run(self, *, all_pending=False):
        unfinished = await self.unfinished()
        retries = await self.retry_orders()
        async with db_session.session_scope() as session:
            orders = list(
                await session.scalars(
                    sa.select(Order)
                    .where(
                        Order.mode == TradingMode.PAPER,
                        Order.role == "ENTRY",
                        ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
                    )
                    .order_by(Order.id)
                )
            )
            known = {order.id for order in orders}
            for identifier in unfinished:
                if identifier not in known:
                    order = await session.get(Order, identifier)
                    if order is None or order.mode != TradingMode.PAPER or order.role != "ENTRY":
                        raise SafetyError("CANCEL_INTENT_ORDER_UNAVAILABLE")
                    orders.append(order)
        resolved = True
        for order in orders:
            age = (
                self.clock.utcnow() - _utc(order.submitted_at or order.created_at)
            ).total_seconds()
            if (
                age >= 0
                and order.id not in unfinished
                and order.id not in retries
                and not all_pending
                and age < self.executor.settings.paper_entry_max_age_seconds
            ):
                continue
            previous = unfinished.get(order.id)
            chain = previous.chain_id if previous else new_id("swp")
            reason = (
                previous.result["reason"]
                if previous
                else retries[order.id]
                if order.id in retries
                else "SESSION_CUTOFF"
                if all_pending
                else "STALE_ENTRY"
            )
            if previous is None:
                await self.record(
                    order,
                    chain,
                    "ENTRY_CANCEL_INTENT",
                    {
                        "reason": reason,
                        "age_seconds": age,
                        "max_age_seconds": self.executor.settings.paper_entry_max_age_seconds,
                        "status_before": order.status.value,
                    },
                )
            error_code = None
            try:
                if age < 0:
                    raise SafetyError("FUTURE_ORDER_TIMESTAMP")
                await self.executor.cancel(order.id)
            except Exception as error:
                error_code = (
                    error.message if isinstance(error, SafetyError) else type(error).__name__
                )
            async with db_session.session_scope() as session:
                current = await session.get(Order, order.id)
            terminal = current is not None and current.status.is_terminal
            await self.record(
                order,
                chain,
                "ENTRY_CANCEL_RESULT",
                {
                    "reason": reason,
                    "status": current.status.value if current else "MISSING",
                    "filled_quantity": current.filled_quantity if current else None,
                    "terminal": terminal,
                    "error": error_code,
                },
            )
            resolved = resolved and terminal and error_code is None
        if not resolved:
            self.executor.gate.block("paper_order_hygiene", "PAPER_ORDER_CANCELLATION_UNRESOLVED")
        else:
            self.executor.gate.clear("paper_order_hygiene")
        return resolved

    async def retry_orders(self):
        async with db_session.session_scope() as session:
            outcomes = list(
                await session.scalars(
                    sa.select(AuditEvent)
                    .join(Order, Order.id == AuditEvent.order_id)
                    .where(
                        AuditEvent.mode == TradingMode.PAPER,
                        AuditEvent.event_type == "ENTRY_CANCEL_RESULT",
                        Order.mode == TradingMode.PAPER,
                        Order.role == "ENTRY",
                        ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
                    )
                )
            )
        pending = {}
        for outcome in outcomes:
            if not await AuditService(self.clock).verify(outcome.chain_id, expected_count=2):
                raise SafetyError("CANCEL_RESULT_INTEGRITY_FAILURE")
            if not outcome.result.get("terminal") or outcome.result.get("error"):
                pending[outcome.order_id] = outcome.result["reason"]
        return pending

    async def unfinished(self):
        result = sa.orm.aliased(AuditEvent)
        async with db_session.session_scope() as session:
            intents = list(
                await session.scalars(
                    sa.select(AuditEvent).where(
                        AuditEvent.mode == TradingMode.PAPER,
                        AuditEvent.event_type == "ENTRY_CANCEL_INTENT",
                        ~sa.exists(
                            sa.select(result.id).where(
                                result.chain_id == AuditEvent.chain_id,
                                result.event_type == "ENTRY_CANCEL_RESULT",
                            )
                        ),
                    )
                )
            )
        pending = {}
        for intent in intents:
            if not await AuditService(self.clock).verify(intent.chain_id, expected_count=1):
                raise SafetyError("CANCEL_INTENT_INTEGRITY_FAILURE")
            if intent.order_id in pending or intent.result.get("reason") not in {
                "STALE_ENTRY",
                "SESSION_CUTOFF",
                "OWNER_CANCEL",
            }:
                raise SafetyError("CANCEL_INTENT_AMBIGUOUS")
            pending[intent.order_id] = intent
        return pending

    async def record(self, order, chain, event, result, *, actor="paper_order_hygiene"):
        async with db_session.session_scope() as session:
            recorded = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=chain,
                    event_type=event,
                    actor=actor,
                    mode=TradingMode.PAPER,
                ),
                {
                    "order_id": order.id,
                    "proposal_id": order.proposal_id,
                    "instrument_id": order.instrument_id,
                    "result": result,
                },
            )
            if event == "ENTRY_CANCEL_RESULT":
                await record_order_action(session, order, recorded, self.clock)
