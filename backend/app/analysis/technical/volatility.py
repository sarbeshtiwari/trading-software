"""Volatility indicators — TA-004.

ATR (Wilder), Bollinger Bands, Keltner Channels and realised volatility.

ATR matters more here than anywhere else in the system: stop distances are
expressed in ATR multiples, so an ATR that is 10% too small makes every stop 10%
too tight and turns winning trades into stop-outs. It uses true range (which
accounts for gaps) and Wilder smoothing, not a simple average of high-low.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import (
    Values,
    closes,
    register_indicator,
    require_history,
    rolling,
    true_ranges,
    wilder_smooth,
)
from app.analysis.technical.ma import ema, sma
from app.core.errors import ValidationError
from app.marketdata.models import Bar

__all__ = [
    "atr",
    "atr_percent",
    "BollingerResult",
    "bollinger_bands",
    "KeltnerResult",
    "keltner_channels",
    "historical_volatility",
    "standard_deviation",
]


@register_indicator("atr", min_lookback=lambda period=14: period + 1, category="volatility")
def atr(bars: Sequence[Bar], period: int = 14) -> Values:
    """Average True Range using Wilder smoothing."""
    require_history(bars, period + 1, indicator=f"atr({period})")
    ranges = true_ranges(bars)
    # True range is undefined for the first bar, so smoothing seeds at index
    # `period` (the first bar with `period` true ranges behind it).
    return wilder_smooth(ranges, period, seed_index=period)


def atr_percent(bars: Sequence[Bar], period: int = 14) -> Values:
    """ATR as a percentage of close — comparable across instruments."""
    values = atr(bars, period)
    result: Values = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None or bars[index].close == 0:
            continue
        result[index] = value / bars[index].close * Decimal(100)
    return result


def standard_deviation(values: Sequence[Decimal], period: int) -> Values:
    """Population standard deviation over a rolling window.

    Population, not sample: Bollinger Bands are defined on the population form,
    and using the sample form widens every band slightly.
    """
    require_history(values, period, indicator=f"stdev({period})")
    result: Values = [None] * len(values)

    for index, window in rolling(values, period):
        mean = sum(window, Decimal(0)) / Decimal(period)
        variance = sum(((value - mean) ** 2 for value in window), Decimal(0)) / Decimal(period)
        result[index] = variance.sqrt()
    return result


@dataclass(frozen=True)
class BollingerResult:
    middle: Values
    upper: Values
    lower: Values
    bandwidth: Values
    percent_b: Values


@register_indicator(
    "bollinger",
    min_lookback=lambda period=20, deviations=2: period,
    category="volatility",
)
def bollinger_bands(
    values: Sequence[Decimal],
    period: int = 20,
    deviations: Decimal = Decimal(2),
) -> BollingerResult:
    """Bollinger Bands with bandwidth and %B."""
    require_history(values, period, indicator=f"bollinger({period})")
    middle = sma(values, period)
    deviation = standard_deviation(values, period)

    upper: Values = [None] * len(values)
    lower: Values = [None] * len(values)
    bandwidth: Values = [None] * len(values)
    percent_b: Values = [None] * len(values)

    for index in range(len(values)):
        centre = middle[index]
        spread = deviation[index]
        if centre is None or spread is None:
            continue
        band = spread * Decimal(deviations)
        upper[index] = centre + band
        lower[index] = centre - band
        if centre != 0:
            bandwidth[index] = (upper[index] - lower[index]) / centre * Decimal(100)
        width = upper[index] - lower[index]
        if width != 0:
            percent_b[index] = (values[index] - lower[index]) / width

    return BollingerResult(
        middle=middle, upper=upper, lower=lower, bandwidth=bandwidth, percent_b=percent_b
    )


@dataclass(frozen=True)
class KeltnerResult:
    middle: Values
    upper: Values
    lower: Values


@register_indicator(
    "keltner",
    min_lookback=lambda period=20, atr_period=10, multiplier=2: max(period, atr_period + 1),
    category="volatility",
)
def keltner_channels(
    bars: Sequence[Bar],
    period: int = 20,
    atr_period: int = 10,
    multiplier: Decimal = Decimal(2),
) -> KeltnerResult:
    """Keltner Channels: an EMA centre with ATR-width bands."""
    require_history(
        bars, max(period, atr_period + 1), indicator=f"keltner({period},{atr_period})"
    )
    centre = ema(closes(bars), period)
    ranges = atr(bars, atr_period)

    upper: Values = [None] * len(bars)
    lower: Values = [None] * len(bars)
    for index in range(len(bars)):
        if centre[index] is None or ranges[index] is None:
            continue
        band = ranges[index] * Decimal(multiplier)
        upper[index] = centre[index] + band
        lower[index] = centre[index] - band

    return KeltnerResult(middle=centre, upper=upper, lower=lower)


@register_indicator(
    "historical_volatility",
    min_lookback=lambda period=20, periods_per_year=252: period + 1,
    category="volatility",
)
def historical_volatility(
    values: Sequence[Decimal],
    period: int = 20,
    periods_per_year: int = 252,
) -> Values:
    """Annualised realised volatility from log returns, in percent.

    ``periods_per_year`` must match the bar interval: 252 for daily bars, and
    252 * bars-per-session for intraday. Getting it wrong scales the whole series.
    """
    require_history(values, period + 1, indicator=f"historical_volatility({period})")
    if periods_per_year <= 0:
        raise ValidationError("periods_per_year must be positive")

    returns: Values = [None]
    for index in range(1, len(values)):
        previous = values[index - 1]
        if previous <= 0 or values[index] <= 0:
            returns.append(None)
            continue
        returns.append(Decimal(str((values[index] / previous).ln())))

    result: Values = [None] * len(values)
    scale = Decimal(periods_per_year).sqrt()

    for index in range(period, len(values)):
        window = returns[index - period + 1 : index + 1]
        if any(value is None for value in window):
            continue
        mean = sum(window, Decimal(0)) / Decimal(period)  # type: ignore[arg-type]
        variance = sum(
            ((value - mean) ** 2 for value in window), Decimal(0)  # type: ignore[operator]
        ) / Decimal(period)
        result[index] = variance.sqrt() * scale * Decimal(100)
    return result
