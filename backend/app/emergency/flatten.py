"""Best-effort PAPER flatten with explicit per-item outcomes, never fake success."""

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.trading import Order, Position
from app.modes import TradingMode


async def flatten(worker, *, actor, reason):
    outcomes = []
    async with worker.cycle_lock:
        async with db_session.session_scope() as session:
            orders = list(
                (
                    await session.scalars(
                        sa.select(Order).where(
                            Order.mode == TradingMode.PAPER,
                            Order.role == "ENTRY",
                        )
                    )
                ).all()
            )
        for order in orders:
            if order.status.is_terminal:
                continue
            try:
                await worker.executor.cancel(order.id)
                async with db_session.session_scope() as session:
                    cancelled = await session.get(Order, order.id)
                outcomes.append(
                    {"kind": "CANCEL", "id": order.id, "status": cancelled.status.value}
                )
            except Exception as error:
                outcomes.append(
                    {
                        "kind": "CANCEL",
                        "id": order.id,
                        "status": "FAILED",
                        "error": error.message
                        if isinstance(error, SafetyError)
                        else type(error).__name__,
                    }
                )
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER,
                            Position.net_quantity != 0,
                        )
                    )
                ).all()
            )
        for position in positions:
            try:
                identifier = await worker.executor.exit(
                    position.id, ExitReason.EMERGENCY, retry_terminal=True
                )
                async with db_session.session_scope() as session:
                    exit_order = await session.get(Order, identifier)
                    remaining = await session.get(Position, position.id)
                outcomes.append(
                    {
                        "kind": "EXIT",
                        "id": position.id,
                        "order_id": identifier,
                        "status": exit_order.status.value,
                        "remaining_quantity": str(remaining.net_quantity),
                    }
                )
            except Exception as error:
                outcomes.append(
                    {
                        "kind": "EXIT",
                        "id": position.id,
                        "status": "FAILED",
                        "error": error.message
                        if isinstance(error, SafetyError)
                        else type(error).__name__,
                    }
                )
        await AuditService(worker.clock).append(
            AuditIdentity(
                chain_id=new_id("emg"),
                event_type="EMERGENCY_FLATTEN_RESULT",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {"result": {"reason": reason, "outcomes": outcomes}},
        )
    return outcomes
