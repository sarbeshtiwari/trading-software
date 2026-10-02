"""Price-action features — TA-009.

Body and wick geometry, inside/outside bars, range expansion and consolidation.

These are deliberately *features*, not signals. Each returns a measurable
quantity a strategy can test against a threshold it declares, rather than a
built-in verdict like "this is a hammer" — which would hide the threshold inside
the indicator where no backtest can vary it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import Values, register_indicator, require_history
from app.marketdata.models import Bar

__all__ = [
    "BarGeometry",
    "geometry",
    "is_inside_bar",
    "is_outside_bar",
    "range_expansion",
    "consolidation_score",
    "gap_percent",
]


@dataclass(frozen=True)
class BarGeometry:
    """Proportions of a single bar. All ratios are of the full range."""

    body: Decimal
    upper_wick: Decimal
    lower_wick: Decimal
    range: Decimal
    body_ratio: Decimal
    upper_wick_ratio: Decimal
    lower_wick_ratio: Decimal
    is_bullish: bool

    @property
    def close_position(self) -> Decimal:
        """Where the close sits in the range: 0 at the low, 1 at the high."""
        return self.lower_wick_ratio + (
            self.body_ratio if self.is_bullish else Decimal(0)
        )


def geometry(bar: Bar) -> BarGeometry:
    span = bar.high - bar.low
    body = abs(bar.close - bar.open)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low

    if span == 0:
        # A bar with no range (a limit-locked instrument) has no proportions;
        # zeros are the truthful answer rather than a division error.
        zero = Decimal(0)
        return BarGeometry(
            body=zero,
            upper_wick=zero,
            lower_wick=zero,
            range=zero,
            body_ratio=zero,
            upper_wick_ratio=zero,
            lower_wick_ratio=zero,
            is_bullish=bar.close >= bar.open,
        )

    return BarGeometry(
        body=body,
        upper_wick=upper,
        lower_wick=lower,
        range=span,
        body_ratio=body / span,
        upper_wick_ratio=upper / span,
        lower_wick_ratio=lower / span,
        is_bullish=bar.close >= bar.open,
    )


@register_indicator("inside_bar", min_lookback=lambda: 2, category="price_action")
def is_inside_bar(bars: Sequence[Bar]) -> list[Optional[bool]]:
    """True where a bar's entire range sits inside its predecessor's."""
    require_history(bars, 2, indicator="inside_bar")
    result: list[Optional[bool]] = [None]
    for index in range(1, len(bars)):
        previous, current = bars[index - 1], bars[index]
        result.append(current.high <= previous.high and current.low >= previous.low)
    return result


@register_indicator("outside_bar", min_lookback=lambda: 2, category="price_action")
def is_outside_bar(bars: Sequence[Bar]) -> list[Optional[bool]]:
    """True where a bar engulfs its predecessor's range."""
    require_history(bars, 2, indicator="outside_bar")
    result: list[Optional[bool]] = [None]
    for index in range(1, len(bars)):
        previous, current = bars[index - 1], bars[index]
        result.append(current.high >= previous.high and current.low <= previous.low)
    return result


@register_indicator(
    "range_expansion", min_lookback=lambda period=20: period + 1, category="price_action"
)
def range_expansion(bars: Sequence[Bar], period: int = 20) -> Values:
    """Current bar range as a multiple of the average of the prior ``period``."""
    require_history(bars, period + 1, indicator=f"range_expansion({period})")
    result: Values = [None] * len(bars)

    for index in range(period, len(bars)):
        window = bars[index - period : index]
        average = sum(((bar.high - bar.low) for bar in window), Decimal(0)) / Decimal(period)
        if average == 0:
            continue
        result[index] = (bars[index].high - bars[index].low) / average
    return result


@register_indicator(
    "consolidation", min_lookback=lambda period=20: period, category="price_action"
)
def consolidation_score(bars: Sequence[Bar], period: int = 20) -> Values:
    """How tight the recent range is, as a percentage of price.

    Low values mean coiled price. Expressed in percent so it is comparable across
    instruments trading at very different absolute levels.
    """
    require_history(bars, period, indicator=f"consolidation({period})")
    result: Values = [None] * len(bars)

    for index in range(period - 1, len(bars)):
        window = bars[index - period + 1 : index + 1]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        midpoint = (highest + lowest) / Decimal(2)
        if midpoint == 0:
            continue
        result[index] = (highest - lowest) / midpoint * Decimal(100)
    return result


def gap_percent(previous_close: Decimal, current_open: Decimal) -> Decimal:
    """Opening gap as a percentage of the previous close (EQ-006)."""
    if previous_close == 0:
        return Decimal(0)
    return (current_open - previous_close) / previous_close * Decimal(100)
