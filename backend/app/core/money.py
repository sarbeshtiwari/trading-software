"""Money and price arithmetic — ARCH-014.

Rules enforced here:

* All monetary values are :class:`decimal.Decimal`. ``float`` is rejected at the
  boundary rather than silently converted, because ``0.1 + 0.2 != 0.3`` in binary
  floating point and a trading system accumulates those errors into real money.
* Currency amounts quantise to paise (2 dp) with ``ROUND_HALF_UP`` — the rounding
  a human expects, not banker's rounding.
* Prices quantise to the *instrument's* tick size, with an explicit direction,
  because rounding a limit price the wrong way changes execution (EXCH-004).
* Quantities are integers, and F&O quantities are integer multiples of lot size,
  always rounded **down** (SIZE-002) — never round up into more risk.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, localcontext
from typing import Literal, Union

__all__ = [
    "PAISE",
    "ZERO",
    "Numeric",
    "to_decimal",
    "money",
    "quantize_money",
    "round_to_tick",
    "percent_of",
    "pct_change",
    "lots_to_quantity",
    "quantity_to_lots",
    "floor_to_lot",
    "safe_div",
]

#: Smallest currency increment used for quantisation.
PAISE = Decimal("0.01")
ZERO = Decimal("0")

#: Values accepted at the boundary. ``float`` is deliberately excluded.
Numeric = Union[Decimal, int, str]

RoundDirection = Literal["nearest", "up", "down"]

_MODES = {"nearest": ROUND_HALF_UP, "up": ROUND_UP, "down": ROUND_DOWN}


class FloatNotAllowedError(TypeError):
    """Raised when a ``float`` is used where money or a price is expected."""


def to_decimal(value: Numeric, *, field: str = "value") -> Decimal:
    """Coerce ``value`` to :class:`Decimal`, rejecting ``float``.

    ``bool`` is rejected too: ``True`` is an ``int`` in Python and a boolean
    reaching a money field is always a bug.
    """
    if isinstance(value, bool):
        raise FloatNotAllowedError(f"{field}: bool is not a valid numeric value")
    if isinstance(value, float):
        raise FloatNotAllowedError(
            f"{field}: float is not permitted in money/price arithmetic "
            f"(got {value!r}). Pass a Decimal or a string such as Decimal('123.45')."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            raise ValueError(f"{field}: {value!r} is not a valid decimal") from exc
    raise TypeError(f"{field}: unsupported type {type(value).__name__}")


def quantize_money(value: Numeric, *, field: str = "amount") -> Decimal:
    """Quantise a currency amount to paise using ROUND_HALF_UP."""
    return to_decimal(value, field=field).quantize(PAISE, rounding=ROUND_HALF_UP)


#: Short alias used across the codebase for "make this a currency amount".
money = quantize_money


def round_to_tick(
    price: Numeric,
    tick_size: Numeric,
    *,
    direction: RoundDirection = "nearest",
    field: str = "price",
) -> Decimal:
    """Round ``price`` to a multiple of ``tick_size`` (EXCH-004).

    ``direction`` must be chosen deliberately:

    * ``"down"`` for a buy limit price (never pay more than intended),
    * ``"up"`` for a sell limit price,
    * ``"nearest"`` for reference/display prices.
    """
    p = to_decimal(price, field=field)
    tick = to_decimal(tick_size, field="tick_size")
    if tick <= 0:
        raise ValueError(f"tick_size must be positive, got {tick}")
    if direction not in _MODES:
        raise ValueError(f"direction must be one of {tuple(_MODES)}, got {direction!r}")
    with localcontext() as ctx:
        ctx.prec = 28
        steps = (p / tick).quantize(Decimal("1"), rounding=_MODES[direction])
        return quantize_money(steps * tick, field=field)


def percent_of(value: Numeric, percent: Numeric, *, field: str = "value") -> Decimal:
    """Return ``percent`` percent of ``value``, quantised to paise.

    ``percent`` is expressed in percentage points: ``percent_of(100000, "0.5")``
    is 500 — matching how risk limits are configured (0.5% per trade).
    """
    base = to_decimal(value, field=field)
    pct = to_decimal(percent, field="percent")
    return quantize_money(base * pct / Decimal(100), field=field)


def pct_change(new: Numeric, old: Numeric) -> Decimal:
    """Percentage change from ``old`` to ``new``, to 4 dp. Zero base returns 0."""
    n = to_decimal(new, field="new")
    o = to_decimal(old, field="old")
    if o == 0:
        return ZERO
    return ((n - o) / o * Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def lots_to_quantity(lots: int, lot_size: int) -> int:
    """Convert a lot count to an order quantity."""
    if lots < 0:
        raise ValueError(f"lots must be non-negative, got {lots}")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    return lots * lot_size


def quantity_to_lots(quantity: int, lot_size: int) -> int:
    """Convert a quantity to whole lots, rejecting non-multiples."""
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if quantity % lot_size != 0:
        raise ValueError(f"quantity {quantity} is not a multiple of lot size {lot_size}")
    return quantity // lot_size


def floor_to_lot(quantity: int, lot_size: int) -> int:
    """Round a quantity DOWN to a whole number of lots (SIZE-002).

    Always downward: rounding up would take more risk than the sizer allowed.
    """
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if quantity < 0:
        raise ValueError(f"quantity must be non-negative, got {quantity}")
    return (quantity // lot_size) * lot_size


def safe_div(numerator: Numeric, denominator: Numeric, *, default: Decimal = ZERO) -> Decimal:
    """Divide, returning ``default`` when the denominator is zero."""
    d = to_decimal(denominator, field="denominator")
    if d == 0:
        return default
    return to_decimal(numerator, field="numerator") / d
