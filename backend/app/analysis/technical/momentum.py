"""Momentum indicators — TA-003.

RSI (Wilder), MACD, Stochastic and Rate of Change.

RSI uses **Wilder's smoothing**, not an EMA of the same period. The two differ by
a factor of roughly two in responsiveness, and using an EMA produces readings
that disagree with every chart the owner will compare against.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import (
    Values,
    register_indicator,
    require_history,
    rolling,
    wilder_smooth,
)
from app.analysis.technical.ma import ema
from app.marketdata.models import Bar

__all__ = ["rsi", "macd", "MACDResult", "stochastic", "StochasticResult", "roc"]


@register_indicator("rsi", min_lookback=lambda period=14: period + 1, category="momentum")
def rsi(values: Sequence[Decimal], period: int = 14) -> Values:
    """Wilder's Relative Strength Index.

    Returns 100 when there are no losses in the window — the mathematically
    correct limit, rather than a division error or an arbitrary cap.
    """
    require_history(values, period + 1, indicator=f"rsi({period})")

    gains: Values = [None]
    losses: Values = [None]
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, Decimal(0)))
        losses.append(max(-change, Decimal(0)))

    average_gain = wilder_smooth(gains, period, seed_index=period)
    average_loss = wilder_smooth(losses, period, seed_index=period)

    result: Values = [None] * len(values)
    for index in range(len(values)):
        gain = average_gain[index]
        loss = average_loss[index]
        if gain is None or loss is None:
            continue
        if loss == 0:
            result[index] = Decimal(100)
        else:
            rs = gain / loss
            result[index] = Decimal(100) - (Decimal(100) / (Decimal(1) + rs))
    return result


@dataclass(frozen=True)
class MACDResult:
    macd: Values
    signal: Values
    histogram: Values


@register_indicator(
    "macd",
    min_lookback=lambda fast=12, slow=26, signal=9: slow + signal,
    category="momentum",
)
def macd(
    values: Sequence[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """MACD line, signal line and histogram."""
    if fast >= slow:
        from app.core.errors import ValidationError

        raise ValidationError(f"macd: fast ({fast}) must be shorter than slow ({slow})")
    require_history(values, slow + signal, indicator=f"macd({fast},{slow},{signal})")

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    macd_line: Values = [
        (fast_ema[index] - slow_ema[index])
        if fast_ema[index] is not None and slow_ema[index] is not None
        else None
        for index in range(len(values))
    ]

    # The signal line is an EMA of the MACD line, which only starts once the MACD
    # line exists; feeding the Nones through would shift it.
    present = [value for value in macd_line if value is not None]
    signal_line: Values = [None] * len(values)
    if len(present) >= signal:
        signal_values = ema(present, signal)
        offset = len(values) - len(present)
        for index, value in enumerate(signal_values):
            signal_line[offset + index] = value

    histogram: Values = [
        (macd_line[index] - signal_line[index])
        if macd_line[index] is not None and signal_line[index] is not None
        else None
        for index in range(len(values))
    ]
    return MACDResult(macd=macd_line, signal=signal_line, histogram=histogram)


@dataclass(frozen=True)
class StochasticResult:
    k: Values
    d: Values


@register_indicator(
    "stochastic",
    min_lookback=lambda period=14, smooth_k=3, smooth_d=3: period + smooth_k + smooth_d,
    category="momentum",
)
def stochastic(
    bars: Sequence[Bar],
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> StochasticResult:
    """Stochastic oscillator (%K smoothed, %D as the average of %K)."""
    require_history(bars, period, indicator=f"stochastic({period})")

    raw_k: Values = [None] * len(bars)
    for index in range(period - 1, len(bars)):
        window = bars[index - period + 1 : index + 1]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        span = highest - lowest
        if span == 0:
            # A flat window has no position within a range; 50 is the neutral
            # reading rather than a division by zero.
            raw_k[index] = Decimal(50)
        else:
            raw_k[index] = (bars[index].close - lowest) / span * Decimal(100)

    k = _smooth(raw_k, smooth_k)
    d = _smooth(k, smooth_d)
    return StochasticResult(k=k, d=d)


def _smooth(values: Values, period: int) -> Values:
    """Simple average over the present values only."""
    if period <= 1:
        return list(values)
    result: Values = [None] * len(values)
    for index in range(len(values)):
        window = values[max(0, index - period + 1) : index + 1]
        if len(window) < period or any(value is None for value in window):
            continue
        result[index] = sum(window, Decimal(0)) / Decimal(period)  # type: ignore[arg-type]
    return result


@register_indicator("roc", min_lookback=lambda period=12: period + 1, category="momentum")
def roc(values: Sequence[Decimal], period: int = 12) -> Values:
    """Rate of change, in percent."""
    require_history(values, period + 1, indicator=f"roc({period})")
    result: Values = [None] * len(values)
    for index in range(period, len(values)):
        previous = values[index - period]
        if previous == 0:
            continue
        result[index] = (values[index] - previous) / previous * Decimal(100)
    return result
