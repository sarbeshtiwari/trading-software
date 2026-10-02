"""Volume indicators — TA-005.

OBV, volume moving average, relative volume and VWAP deviation.

Relative volume needs care intraday: comparing the current *partial* session's
volume against completed sessions makes every morning look quiet and every
afternoon look busy. The session-aware variant compares like with like.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import (
    Values,
    closes,
    register_indicator,
    require_history,
    rolling,
)
from app.analysis.technical.ma import vwap
from app.core.clock import ensure_ist
from app.marketdata.models import Bar

__all__ = [
    "obv",
    "volume_sma",
    "relative_volume",
    "vwap_deviation",
    "session_relative_volume",
]


@register_indicator("obv", min_lookback=lambda: 2, category="volume")
def obv(bars: Sequence[Bar]) -> Values:
    """On-Balance Volume, starting at zero on the first bar."""
    require_history(bars, 2, indicator="obv")
    result: Values = [Decimal(0)]
    running = Decimal(0)

    for index in range(1, len(bars)):
        change = bars[index].close - bars[index - 1].close
        volume = Decimal(bars[index].volume)
        if change > 0:
            running += volume
        elif change < 0:
            running -= volume
        result.append(running)
    return result


@register_indicator("volume_sma", min_lookback=lambda period=20: period, category="volume")
def volume_sma(bars: Sequence[Bar], period: int = 20) -> Values:
    """Simple moving average of volume."""
    require_history(bars, period, indicator=f"volume_sma({period})")
    values = [Decimal(bar.volume) for bar in bars]
    result: Values = [None] * len(bars)
    for index, window in rolling(values, period):
        result[index] = sum(window, Decimal(0)) / Decimal(period)
    return result


@register_indicator(
    "relative_volume", min_lookback=lambda period=20: period + 1, category="volume"
)
def relative_volume(bars: Sequence[Bar], period: int = 20) -> Values:
    """Current volume as a multiple of its recent average.

    The average **excludes** the current bar: including it damps exactly the
    spike the indicator exists to detect.
    """
    require_history(bars, period + 1, indicator=f"relative_volume({period})")
    result: Values = [None] * len(bars)

    for index in range(period, len(bars)):
        window = bars[index - period : index]
        average = sum((Decimal(bar.volume) for bar in window), Decimal(0)) / Decimal(period)
        if average == 0:
            continue
        result[index] = Decimal(bars[index].volume) / average
    return result


def session_relative_volume(bars: Sequence[Bar], sessions: int = 5) -> Values:
    """Cumulative session volume against the same point in prior sessions.

    Compares like with like: 10:30 today against 10:30 on previous days, rather
    than against whole-day totals.
    """
    if sessions < 1:
        from app.core.errors import ValidationError

        raise ValidationError("sessions must be at least 1")

    cumulative: list[Decimal] = []
    minute_of_session: list[int] = []
    running = Decimal(0)
    current_day = None
    session_start = None

    for bar in bars:
        moment = ensure_ist(bar.ts)
        day = moment.date()
        if day != current_day:
            current_day = day
            running = Decimal(0)
            session_start = moment
        running += Decimal(bar.volume)
        cumulative.append(running)
        minute_of_session.append(
            int((moment - session_start).total_seconds() // 60) if session_start else 0
        )

    result: Values = [None] * len(bars)
    for index in range(len(bars)):
        marker = minute_of_session[index]
        today = ensure_ist(bars[index].ts).date()
        comparable = [
            cumulative[other]
            for other in range(index)
            if minute_of_session[other] == marker
            and ensure_ist(bars[other].ts).date() != today
        ][-sessions:]
        if not comparable:
            continue
        average = sum(comparable, Decimal(0)) / Decimal(len(comparable))
        if average == 0:
            continue
        result[index] = cumulative[index] / average
    return result


@register_indicator("vwap_deviation", min_lookback=lambda: 1, category="volume")
def vwap_deviation(bars: Sequence[Bar]) -> Values:
    """Distance of close from session VWAP, in percent."""
    require_history(bars, 1, indicator="vwap_deviation")
    reference = vwap(bars)
    result: Values = [None] * len(bars)

    for index, bar in enumerate(bars):
        anchor = reference[index]
        if anchor is None or anchor == 0:
            continue
        result[index] = (bar.close - anchor) / anchor * Decimal(100)
    return result
