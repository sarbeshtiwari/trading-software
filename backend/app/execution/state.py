"""Persist legal OMS transitions in the caller's transaction."""

from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db.models.trading import OrderEvent

ACTIVE = {OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}
TERMINAL = {OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
ALLOWED = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED},
    OrderStatus.SUBMITTED: ACTIVE | TERMINAL | {OrderStatus.UNKNOWN},
    OrderStatus.OPEN: ACTIVE | TERMINAL | {OrderStatus.UNKNOWN},
    OrderStatus.PENDING: ACTIVE | TERMINAL | {OrderStatus.UNKNOWN},
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.EXECUTED,
        OrderStatus.CANCELLED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.UNKNOWN: ACTIVE | TERMINAL,
}


def transition(session, order, status, now, *, source, detail=None):
    if status == order.status:
        return
    if status not in ALLOWED.get(order.status, set()):
        raise SafetyError(f"Illegal order transition: {order.status} -> {status}")
    session.add(
        OrderEvent(
            order_id=order.id,
            from_status=order.status,
            to_status=status,
            source=source,
            detail=detail,
            occurred_at=now,
        )
    )
    order.status = status
    if status.is_terminal:
        order.completed_at = now
