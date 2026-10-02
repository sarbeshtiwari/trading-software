"""Permitted committed PAPER audit facts and their transport event types."""

from app.core.events import EventType

PAPER_EVENT_TYPES = {
    "ORDER_CREATED": EventType.ORDER_UPDATE,
    "ORDER_SUBMITTED": EventType.ORDER_SUBMITTED,
    "ORDER_SYNCHRONIZED": EventType.ORDER_UPDATE,
    "ORDER_UNKNOWN": EventType.ORDER_UPDATE,
    "PAPER_FILL_RECORDED": EventType.FILL,
    "PAPER_POSITION_UPDATED": EventType.POSITION_UPDATE,
    "PAPER_POSITION_CLOSED": EventType.POSITION_CLOSED,
}
