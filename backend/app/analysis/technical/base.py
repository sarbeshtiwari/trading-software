"""Indicator framework — TA-001.

Every indicator in this system is a **pure function** over a price series that
returns a list aligned 1:1 with its input, with ``None`` wherever there is not
yet enough history to produce a value.

Three rules, each of which exists because the alternative silently produces wrong
numbers rather than errors:

1. **Alignment.** ``result[i]`` always corresponds to ``bars[i]``. Indicators that
   return a shorter list force every caller to re-derive the offset, and one of
   them will get it wrong.
2. **No fabricated warm-up.** The first ``period-1`` values are ``None``, not the
   seed value repeated. An EMA that reports its seed as a real reading will have
   a strategy trading a line that does not exist yet.
3. **Declared lookback.** Each indicator states the minimum bars it needs.
   :func:`require_history` turns a shortfall into an exception, because an
   indicator fed too little data does not fail — it returns a plausible number.

Decimal throughout, matching ARCH-014: indicator values feed directly into stop
distances and position sizes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Callable, Iterable, Optional, Sequence

from app.core.errors import InsufficientHistoryError, ValidationError
from app.marketdata.models import Bar

# Indicator maths needs more precision than money; results are quantised by the
# caller when they become prices.
getcontext().prec = 28

__all__ = [
    "Values",
    "Series",
    "IndicatorSpec",
    "registry",
    "register_indicator",
    "require_history",
    "closes",
    "highs",
    "lows",
    "opens",
    "volumes",
    "typical_prices",
    "true_ranges",
    "wilder_smooth",
    "rolling",
    "last_value",
    "to_decimal_list",
]

#: An indicator result: one slot per input bar, ``None`` until warm.
Values = list[Optional[Decimal]]
Series = Sequence[Decimal]


@dataclass(frozen=True)
class IndicatorSpec:
    """What an indicator is and what it needs."""

    name: str
    #: Minimum bars required, given its parameters.
    min_lookback: Callable[..., int]
    category: str = "general"
    description: str = ""


class IndicatorRegistry:
    """Every indicator the system can compute.

    Used by the no-orphan test (TA-012): an indicator nothing consumes is
    complexity with no justification, and the specification forbids it.
    """

    def __init__(self) -> None:
        self._specs: dict[str, IndicatorSpec] = {}

    def add(self, spec: IndicatorSpec) -> None:
        if spec.name in self._specs:
            raise ValidationError(f"indicator {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[IndicatorSpec]:
        return self._specs.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def by_category(self, category: str) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, spec in self._specs.items() if spec.category == category)
        )

    def __len__(self) -> int:
        return len(self._specs)


registry = IndicatorRegistry()


def register_indicator(
    name: str,
    *,
    min_lookback: Callable[..., int],
    category: str = "general",
    description: str = "",
) -> Callable[[Callable], Callable]:
    """Decorator recording an indicator in the registry."""

    def decorate(func: Callable) -> Callable:
        registry.add(
            IndicatorSpec(
                name=name,
                min_lookback=min_lookback,
                category=category,
                description=description or (func.__doc__ or "").strip().split("\n")[0],
            )
        )
        func.indicator_name = name  # type: ignore[attr-defined]
        return func

    return decorate


# --- Guards ---------------------------------------------------------------


def require_history(values: Sequence, needed: int, *, indicator: str) -> None:
    """Raise when the series is too short to compute ``indicator`` at all."""
    if needed <= 0:
        raise ValidationError(f"{indicator}: lookback must be positive, got {needed}")
    if len(values) < needed:
        raise InsufficientHistoryError(
            f"{indicator} needs at least {needed} bars but received {len(values)}",
            context={"indicator": indicator, "needed": needed, "received": len(values)},
        )


def to_decimal_list(values: Iterable) -> list[Decimal]:
    """Coerce a series to Decimal, rejecting float (ARCH-014)."""
    from app.core.money import to_decimal

    return [to_decimal(value, field="series value") for value in values]


# --- Bar accessors --------------------------------------------------------


def closes(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.close for bar in bars]


def highs(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.high for bar in bars]


def lows(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.low for bar in bars]


def opens(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.open for bar in bars]


def volumes(bars: Sequence[Bar]) -> list[int]:
    return [bar.volume for bar in bars]


def typical_prices(bars: Sequence[Bar]) -> list[Decimal]:
    """(H + L + C) / 3 — the price VWAP and CCI are built on."""
    return [(bar.high + bar.low + bar.close) / Decimal(3) for bar in bars]


def true_ranges(bars: Sequence[Bar]) -> Values:
    """True range per bar. The first bar has no previous close, so it is ``None``.

    Using ``high - low`` for the first bar (a common shortcut) understates range
    on a gap open and makes every ATR-derived stop slightly too tight.
    """
    result: Values = [None]
    for index in range(1, len(bars)):
        current = bars[index]
        previous_close = bars[index - 1].close
        result.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )
    return result


# --- Shared maths ---------------------------------------------------------


def rolling(values: Sequence, period: int):
    """Yield ``(index, window)`` for each complete window."""
    for index in range(period - 1, len(values)):
        yield index, values[index - period + 1 : index + 1]


def wilder_smooth(values: Values, period: int, *, seed_index: Optional[int] = None) -> Values:
    """Wilder's smoothing: the average used by RSI, ATR and ADX.

    Distinct from an EMA. Wilder uses ``1/period`` where an EMA of the same period
    uses ``2/(period+1)``; substituting one for the other shifts every RSI reading
    and is a classic source of "my indicator disagrees with the chart".

    The seed is the simple average of the first ``period`` present values.
    """
    result: Values = [None] * len(values)
    present = [index for index, value in enumerate(values) if value is not None]
    if len(present) < period:
        return result

    start = seed_index if seed_index is not None else present[period - 1]
    window = [values[index] for index in present[:period]]
    current = sum(window, Decimal(0)) / Decimal(period)
    result[start] = current

    for index in range(start + 1, len(values)):
        value = values[index]
        if value is None:
            result[index] = current
            continue
        current = (current * Decimal(period - 1) + value) / Decimal(period)
        result[index] = current
    return result


def last_value(values: Values) -> Optional[Decimal]:
    """The most recent non-``None`` value, or ``None`` if the series is cold."""
    for value in reversed(values):
        if value is not None:
            return value
    return None
