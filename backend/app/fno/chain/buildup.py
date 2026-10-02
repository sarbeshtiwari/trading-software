"""OC-004: price/OI matrix using changes over the same two observed snapshots."""

from datetime import timedelta
from decimal import Decimal
from enum import Enum

from app.fno.chain.model import validate_chain
from app.marketdata.models import OptionChain


class BuildUp(str, Enum):
    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    LONG_UNWINDING = "LONG_UNWINDING"
    SHORT_COVERING = "SHORT_COVERING"
    UNCHANGED = "UNCHANGED"
    UNAVAILABLE = "UNAVAILABLE"


def classify(price_change: Decimal | None, oi_change: int | None) -> BuildUp:
    if price_change is None or oi_change is None:
        return BuildUp.UNAVAILABLE
    if not price_change.is_finite() or type(oi_change) is not int:
        raise ValueError("invalid price/OI change")
    if price_change == 0 or oi_change == 0:
        return BuildUp.UNCHANGED
    if oi_change > 0:
        return BuildUp.LONG_BUILDUP if price_change > 0 else BuildUp.SHORT_BUILDUP
    return BuildUp.SHORT_COVERING if price_change > 0 else BuildUp.LONG_UNWINDING


def buildup(
    current: OptionChain,
    previous: OptionChain,
    *,
    max_gap: timedelta,
) -> dict[Decimal, dict[str, BuildUp]]:
    validate_chain(current)
    validate_chain(previous)
    gap = current.observed_at - previous.observed_at
    if (current.underlying, current.expiry, current.data_origin) != (
        previous.underlying,
        previous.expiry,
        previous.data_origin,
    ) or not timedelta(0) < gap <= max_gap:
        raise ValueError("incompatible, future or stale comparison snapshot")
    old = {row.strike: row for row in previous.strikes}
    result = {}
    for row in current.strikes:
        sides = {}
        for side in ("call", "put"):
            latest = getattr(row, side)
            prior = getattr(old.get(row.strike), side, None)
            if (
                latest is None
                or prior is None
                or latest.trading_symbol != prior.trading_symbol
                or latest.ltp is None
                or prior.ltp is None
                or latest.open_interest is None
                or prior.open_interest is None
            ):
                sides[side] = BuildUp.UNAVAILABLE
            else:
                sides[side] = classify(
                    latest.ltp - prior.ltp, latest.open_interest - prior.open_interest
                )
        result[row.strike] = sides
    return result
