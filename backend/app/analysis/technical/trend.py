"""Trend indicators — TA-006.

ADX/DI (Wilder), Supertrend and Donchian channels.

ADX is the regime classifier's main input (REG-001), so its construction is worth
stating: directional movement is *exclusive* — on any given bar at most one of
+DM and -DM is non-zero. Computing both from raw high/low differences without
that rule produces an ADX that never falls, and a regime detector that thinks
every market is trending.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import (
    Values,
    register_indicator,
    require_history,
    true_ranges,
    wilder_smooth,
)
from app.core.enums import SignalDirection
from app.marketdata.models import Bar

__all__ = [
    "ADXResult",
    "adx",
    "SupertrendResult",
    "supertrend",
    "DonchianResult",
    "donchian_channels",
]


@dataclass(frozen=True)
class ADXResult:
    adx: Values
    plus_di: Values
    minus_di: Values


@register_indicator("adx", min_lookback=lambda period=14: period * 2 + 1, category="trend")
def adx(bars: Sequence[Bar], period: int = 14) -> ADXResult:
    """Average Directional Index with +DI and -DI."""
    require_history(bars, period * 2 + 1, indicator=f"adx({period})")

    plus_dm: Values = [None]
    minus_dm: Values = [None]
    for index in range(1, len(bars)):
        up_move = bars[index].high - bars[index - 1].high
        down_move = bars[index - 1].low - bars[index].low
        # Exclusive by construction: a bar is either directionally up or down.
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else Decimal(0))
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else Decimal(0))

    smoothed_tr = wilder_smooth(true_ranges(bars), period, seed_index=period)
    smoothed_plus = wilder_smooth(plus_dm, period, seed_index=period)
    smoothed_minus = wilder_smooth(minus_dm, period, seed_index=period)

    plus_di: Values = [None] * len(bars)
    minus_di: Values = [None] * len(bars)
    dx: Values = [None] * len(bars)

    for index in range(len(bars)):
        tr = smoothed_tr[index]
        if tr is None or tr == 0:
            continue
        up = smoothed_plus[index]
        down = smoothed_minus[index]
        if up is None or down is None:
            continue
        plus_di[index] = up / tr * Decimal(100)
        minus_di[index] = down / tr * Decimal(100)

        total = plus_di[index] + minus_di[index]
        if total == 0:
            # No directional movement at all. DX is 0/0, and the meaningful
            # reading is zero trend strength, not "unknown": leaving it None
            # would stop ADX seeding and a flat market would report no value.
            dx[index] = Decimal(0)
        else:
            dx[index] = abs(plus_di[index] - minus_di[index]) / total * Decimal(100)

    # ADX is Wilder smoothing of DX, seeded once DX has `period` values.
    present = [index for index, value in enumerate(dx) if value is not None]
    adx_values: Values = [None] * len(bars)
    if len(present) >= period:
        adx_values = wilder_smooth(dx, period, seed_index=present[period - 1])

    return ADXResult(adx=adx_values, plus_di=plus_di, minus_di=minus_di)


@dataclass(frozen=True)
class SupertrendResult:
    value: Values
    direction: list[Optional[SignalDirection]]


@register_indicator(
    "supertrend",
    min_lookback=lambda period=10, multiplier=3: period + 1,
    category="trend",
)
def supertrend(
    bars: Sequence[Bar],
    period: int = 10,
    multiplier: Decimal = Decimal(3),
) -> SupertrendResult:
    """Supertrend line and its direction.

    The band-tightening rule (a band only moves in the favourable direction while
    the trend holds) is what stops the line whipsawing on every bar; without it
    Supertrend is just a moving ATR envelope.
    """
    from app.analysis.technical.volatility import atr

    require_history(bars, period + 1, indicator=f"supertrend({period})")
    ranges = atr(bars, period)

    value: Values = [None] * len(bars)
    direction: list[Optional[SignalDirection]] = [None] * len(bars)

    final_upper: Optional[Decimal] = None
    final_lower: Optional[Decimal] = None
    trend_up: Optional[bool] = None

    for index in range(len(bars)):
        band = ranges[index]
        if band is None:
            continue

        mid = (bars[index].high + bars[index].low) / Decimal(2)
        basic_upper = mid + Decimal(multiplier) * band
        basic_lower = mid - Decimal(multiplier) * band
        close = bars[index].close
        previous_close = bars[index - 1].close if index else close

        if final_upper is None or final_lower is None:
            final_upper, final_lower = basic_upper, basic_lower
            trend_up = close >= basic_lower
        else:
            final_upper = (
                basic_upper
                if basic_upper < final_upper or previous_close > final_upper
                else final_upper
            )
            final_lower = (
                basic_lower
                if basic_lower > final_lower or previous_close < final_lower
                else final_lower
            )
            if trend_up and close < final_lower:
                trend_up = False
            elif not trend_up and close > final_upper:
                trend_up = True

        value[index] = final_lower if trend_up else final_upper
        direction[index] = SignalDirection.LONG if trend_up else SignalDirection.SHORT

    return SupertrendResult(value=value, direction=direction)


@dataclass(frozen=True)
class DonchianResult:
    upper: Values
    lower: Values
    middle: Values


@register_indicator("donchian", min_lookback=lambda period=20: period, category="trend")
def donchian_channels(bars: Sequence[Bar], period: int = 20) -> DonchianResult:
    """Highest high and lowest low over the window, and their midpoint.

    The window **excludes** the current bar: a breakout system comparing the
    current price to a channel that already contains it can never trigger.
    """
    require_history(bars, period + 1, indicator=f"donchian({period})")

    upper: Values = [None] * len(bars)
    lower: Values = [None] * len(bars)
    middle: Values = [None] * len(bars)

    for index in range(period, len(bars)):
        window = bars[index - period : index]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        upper[index] = highest
        lower[index] = lowest
        middle[index] = (highest + lowest) / Decimal(2)

    return DonchianResult(upper=upper, lower=lower, middle=middle)
