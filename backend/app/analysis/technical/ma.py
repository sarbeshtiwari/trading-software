"""Moving averages — TA-002.

SMA, EMA, WMA and session-anchored VWAP.

The VWAP here is **session-anchored**: it resets at the start of each trading day,
because that is what an intraday trader means by VWAP. A rolling VWAP that never
resets drifts away from the day's volume-weighted price and would put every
mean-reversion entry in the wrong place.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import (
    Values,
    register_indicator,
    require_history,
    rolling,
    typical_prices,
)
from app.core.clock import ensure_ist
from app.marketdata.models import Bar

__all__ = ["sma", "ema", "wma", "vwap", "anchored_vwap"]


@register_indicator("sma", min_lookback=lambda period: period, category="ma")
def sma(values: Sequence[Decimal], period: int) -> Values:
    """Simple moving average."""
    require_history(values, period, indicator=f"sma({period})")
    result: Values = [None] * len(values)
    window_sum = sum(values[: period - 1], Decimal(0))

    for index in range(period - 1, len(values)):
        window_sum += values[index]
        result[index] = window_sum / Decimal(period)
        window_sum -= values[index - period + 1]
    return result


@register_indicator("ema", min_lookback=lambda period: period, category="ma")
def ema(values: Sequence[Decimal], period: int) -> Values:
    """Exponential moving average, seeded with the SMA of the first ``period`` bars.

    Seeding with the SMA rather than the first close is the convention charting
    packages use; seeding with the first close makes the early values depend
    entirely on one bar.
    """
    require_history(values, period, indicator=f"ema({period})")
    multiplier = Decimal(2) / Decimal(period + 1)
    result: Values = [None] * len(values)

    seed = sum(values[:period], Decimal(0)) / Decimal(period)
    result[period - 1] = seed
    current = seed

    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


@register_indicator("wma", min_lookback=lambda period: period, category="ma")
def wma(values: Sequence[Decimal], period: int) -> Values:
    """Weighted moving average: weight ``i`` on the i-th most recent bar."""
    require_history(values, period, indicator=f"wma({period})")
    denominator = Decimal(period * (period + 1) // 2)
    result: Values = [None] * len(values)

    for index, window in rolling(values, period):
        weighted = sum(
            (value * Decimal(position + 1) for position, value in enumerate(window)),
            Decimal(0),
        )
        result[index] = weighted / denominator
    return result


@register_indicator("vwap", min_lookback=lambda: 1, category="ma")
def vwap(bars: Sequence[Bar]) -> Values:
    """Session-anchored VWAP. Resets at the start of each trading day."""
    require_history(bars, 1, indicator="vwap")
    prices = typical_prices(bars)

    result: Values = []
    cumulative_pv = Decimal(0)
    cumulative_volume = Decimal(0)
    current_day = None

    for index, bar in enumerate(bars):
        day = ensure_ist(bar.ts).date()
        if day != current_day:
            current_day = day
            cumulative_pv = Decimal(0)
            cumulative_volume = Decimal(0)

        volume = Decimal(bar.volume)
        cumulative_pv += prices[index] * volume
        cumulative_volume += volume

        if cumulative_volume == 0:
            # No volume yet today: the typical price is the best available
            # reference, and pretending otherwise would divide by zero.
            result.append(prices[index])
        else:
            result.append(cumulative_pv / cumulative_volume)
    return result


def anchored_vwap(bars: Sequence[Bar], anchor_index: int) -> Values:
    """VWAP measured from a chosen bar (a swing low, a gap, an event)."""
    if not 0 <= anchor_index < len(bars):
        from app.core.errors import ValidationError

        raise ValidationError(
            f"anchor_index {anchor_index} is outside the series of {len(bars)} bars"
        )

    prices = typical_prices(bars)
    result: Values = [None] * len(bars)
    cumulative_pv = Decimal(0)
    cumulative_volume = Decimal(0)

    for index in range(anchor_index, len(bars)):
        volume = Decimal(bars[index].volume)
        cumulative_pv += prices[index] * volume
        cumulative_volume += volume
        result[index] = (
            prices[index] if cumulative_volume == 0 else cumulative_pv / cumulative_volume
        )
    return result
