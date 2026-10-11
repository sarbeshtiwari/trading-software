"""FNOR-001: derivative exposure measured as underlying notional, never premium.

A cash equity and a futures contract trade at the price that *is* their exposure
basis, so the traded price measures one unit of exposure directly. An option
does not. Its premium is the capital at risk; the exposure it carries belongs to
the underlying, and a premium-based measure understates it by whatever leverage
the contract provides. Measuring an option on its premium would let a position
pass a gross-exposure or concentration limit while carrying many times that
exposure in the underlying, so this module requires an observed underlying price
and refuses to substitute the premium for it.

Every function here is pure: callers supply prices they have already validated
for freshness and provenance, and an absent underlying price fails closed rather
than falling back to a measure that would read as smaller than the real one.
"""

from decimal import Decimal

from app.core.enums import InstrumentType
from app.core.errors import SafetyError

__all__ = ["ExposureUnavailable", "lot_notional", "notional_per_unit"]


class ExposureUnavailable(SafetyError):
    """A notional measure needs an input that was never observed."""


def _observed(value: Decimal | None, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ExposureUnavailable(
            f"EXPOSURE_{field}_UNAVAILABLE", code=f"EXPOSURE_{field}_UNAVAILABLE"
        )
    return value


def notional_per_unit(
    instrument_type: InstrumentType,
    *,
    traded_price: Decimal | None,
    underlying_price: Decimal | None = None,
) -> Decimal:
    """Exposure carried by one unit of the instrument.

    An option takes ``max`` of the underlying and its own premium: the premium
    of a deep in-the-money put can exceed the spot it is written on, and the
    measure must never read as smaller than the capital committed.
    """
    traded = _observed(traded_price, "PRICE")
    if instrument_type != InstrumentType.OPTION:
        return traded
    return max(_observed(underlying_price, "UNDERLYING"), traded)


def lot_notional(lot_size: int, *, underlying_price: Decimal | None) -> Decimal:
    """Notional of a single lot — FNOR-001's lot size x underlying price.

    ``lot_size`` comes from supplied master data; this never guesses one.
    """
    if type(lot_size) is not int or lot_size <= 0:
        raise ExposureUnavailable(
            "EXPOSURE_LOT_SIZE_UNAVAILABLE", code="EXPOSURE_LOT_SIZE_UNAVAILABLE"
        )
    return lot_size * _observed(underlying_price, "UNDERLYING")
