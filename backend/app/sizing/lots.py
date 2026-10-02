"""Lot quantities round down; entry/stop prices widen risk conservatively."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.core.enums import SignalDirection


def round_lots(quantity: Decimal, lot_size: int) -> int:
    if not quantity.is_finite() or quantity < 0 or type(lot_size) is not int or lot_size <= 0:
        raise ValueError("invalid lot rounding inputs")
    return int((quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR)) * lot_size


def protective_ticks(entry: Decimal, stop: Decimal, tick: Decimal, direction: SignalDirection):
    if any(not value.is_finite() or value <= 0 for value in (entry, stop, tick)):
        raise ValueError("positive finite tick prices required")
    if direction not in (SignalDirection.LONG, SignalDirection.SHORT):
        raise ValueError("entry direction required")
    entry_rounding = ROUND_CEILING if direction == SignalDirection.LONG else ROUND_FLOOR
    stop_rounding = ROUND_FLOOR if direction == SignalDirection.LONG else ROUND_CEILING
    adjusted_entry = (entry / tick).to_integral_value(rounding=entry_rounding) * tick
    adjusted_stop = (stop / tick).to_integral_value(rounding=stop_rounding) * tick
    if min(adjusted_entry, adjusted_stop) <= 0:
        raise ValueError("tick rounding produced a nonpositive price")
    return adjusted_entry, adjusted_stop
