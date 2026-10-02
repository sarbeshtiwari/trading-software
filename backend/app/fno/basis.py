"""FUT-003: futures-minus-spot basis and simple ACT/365 annualised carry.

These are pure mathematics, not forecasts or executable prices. Callers must
provide synchronous, point-in-time prices; no financing/dividend rate is assumed.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.fno.chain.model import aware


class Basis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    absolute: Decimal
    fraction: Decimal
    annualised_carry: Decimal


def _price(value: Decimal) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError("positive finite price required")


def futures_basis(spot: Decimal, future: Decimal, *, as_of: datetime, expiry: datetime) -> Basis:
    _price(spot)
    _price(future)
    duration = aware(expiry) - aware(as_of)
    seconds = (
        Decimal(duration.days) * 86400
        + Decimal(duration.seconds)
        + Decimal(duration.microseconds) / 1000000
    )
    if seconds <= 0:
        raise ValueError("future must not be expired")
    fraction = (future - spot) / spot
    return Basis(
        absolute=future - spot,
        fraction=fraction,
        annualised_carry=fraction * Decimal(365 * 86400) / seconds,
    )


def calendar_spread(near: Decimal, far: Decimal) -> Decimal:
    """Far-minus-near price in underlying currency units."""
    _price(near)
    _price(far)
    return far - near
