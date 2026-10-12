"""EXCH-004: no order reaches a broker off-tick or off-lot.

The paper simulator already rejects an off-grid order the way a venue would, but
that models the exchange rather than guarding us: in LIVE the same rejection
arrives only after the order has been sent. This gate runs before any provider
sees the request, so one rule covers both modes.

This gate only ever *rejects*. It cannot correct a price: the dispatch path
asserts that the order still carries the price risk approved, so moving it here
would break that binding. Correction belongs upstream, where it already lives in
`core.money.round_to_tick` and `sizing.lots.protective_ticks`. Nor may quantity
be corrected — silently resizing an order changes the risk the engine approved,
and rounding down can reach zero.
"""

from decimal import Decimal

from app.brokers.models import OrderRequest
from app.core.enums import Segment
from app.core.errors import SafetyError

__all__ = ["ExchangeComplianceError", "enforce"]


class ExchangeComplianceError(SafetyError):
    """The order is not on the exchange's price or size grid."""


def _fail(code: str) -> None:
    raise ExchangeComplianceError(code, code=code)


def _on_grid(value: Decimal, tick: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0 and value % tick == 0


def enforce(request: OrderRequest, *, tick_size: Decimal, lot_size: int) -> None:
    """Raise unless the request is already on the exchange's grid.

    ``tick_size`` and ``lot_size`` come from the instrument master; neither is
    ever guessed, so an unusable value is a missing-evidence failure, not a
    reason to send the order anyway.
    """
    if not isinstance(tick_size, Decimal) or not tick_size.is_finite() or tick_size <= 0:
        _fail("EXCH_TICK_SIZE_UNAVAILABLE")
    if type(lot_size) is not int or lot_size <= 0:
        _fail("EXCH_LOT_SIZE_UNAVAILABLE")
    if type(request.quantity) is not int or request.quantity <= 0:
        _fail("EXCH_QUANTITY_INVALID")
    if request.segment == Segment.FNO and request.quantity % lot_size:
        _fail("EXCH_QUANTITY_NOT_LOT_MULTIPLE")
    if request.price is not None and not _on_grid(request.price, tick_size):
        _fail("EXCH_PRICE_OFF_TICK")
    if request.trigger_price is not None and not _on_grid(request.trigger_price, tick_size):
        _fail("EXCH_TRIGGER_OFF_TICK")
