"""Support, resistance and pivots — TA-007.

Classic and Fibonacci pivots, prior-session high/low/close, and swing detection.

Pivots are computed from the **previous completed session**, never from the
session in progress. A pivot that updates intraday is a level that moves under
the trade, which defeats the purpose of having a level at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import register_indicator, require_history
from app.core.clock import ensure_ist
from app.core.errors import ValidationError
from app.marketdata.models import Bar

__all__ = [
    "SessionLevels",
    "PivotLevels",
    "session_levels",
    "classic_pivots",
    "fibonacci_pivots",
    "Swing",
    "swing_points",
    "nearest_levels",
]


@dataclass(frozen=True)
class SessionLevels:
    """High, low, close and open of one completed session."""

    day: date
    high: Decimal
    low: Decimal
    close: Decimal
    open: Decimal

    @property
    def range(self) -> Decimal:
        return self.high - self.low


@dataclass(frozen=True)
class PivotLevels:
    pivot: Decimal
    r1: Decimal
    r2: Decimal
    r3: Decimal
    s1: Decimal
    s2: Decimal
    s3: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "pivot": self.pivot,
            "r1": self.r1,
            "r2": self.r2,
            "r3": self.r3,
            "s1": self.s1,
            "s2": self.s2,
            "s3": self.s3,
        }

    def nearest_resistance(self, price: Decimal) -> Optional[Decimal]:
        above = sorted(v for v in (self.pivot, self.r1, self.r2, self.r3) if v > price)
        return above[0] if above else None

    def nearest_support(self, price: Decimal) -> Optional[Decimal]:
        below = sorted(
            (v for v in (self.pivot, self.s1, self.s2, self.s3) if v < price), reverse=True
        )
        return below[0] if below else None


def session_levels(bars: Sequence[Bar]) -> dict[date, SessionLevels]:
    """Group bars into sessions and summarise each one."""
    grouped: dict[date, list[Bar]] = {}
    for bar in bars:
        grouped.setdefault(ensure_ist(bar.ts).date(), []).append(bar)

    return {
        day: SessionLevels(
            day=day,
            high=max(bar.high for bar in session),
            low=min(bar.low for bar in session),
            close=session[-1].close,
            open=session[0].open,
        )
        for day, session in grouped.items()
    }


def previous_session(bars: Sequence[Bar], on: date) -> Optional[SessionLevels]:
    """The most recent completed session strictly before ``on``."""
    sessions = session_levels(bars)
    earlier = sorted(day for day in sessions if day < on)
    return sessions[earlier[-1]] if earlier else None


@register_indicator("classic_pivots", min_lookback=lambda: 1, category="levels")
def classic_pivots(previous: SessionLevels) -> PivotLevels:
    """Floor-trader pivots from the previous session."""
    pivot = (previous.high + previous.low + previous.close) / Decimal(3)
    span = previous.range
    return PivotLevels(
        pivot=pivot,
        r1=Decimal(2) * pivot - previous.low,
        s1=Decimal(2) * pivot - previous.high,
        r2=pivot + span,
        s2=pivot - span,
        r3=previous.high + Decimal(2) * (pivot - previous.low),
        s3=previous.low - Decimal(2) * (previous.high - pivot),
    )


@register_indicator("fibonacci_pivots", min_lookback=lambda: 1, category="levels")
def fibonacci_pivots(previous: SessionLevels) -> PivotLevels:
    """Fibonacci pivots: the same centre with 0.382 / 0.618 / 1.000 extensions."""
    pivot = (previous.high + previous.low + previous.close) / Decimal(3)
    span = previous.range
    return PivotLevels(
        pivot=pivot,
        r1=pivot + Decimal("0.382") * span,
        r2=pivot + Decimal("0.618") * span,
        r3=pivot + span,
        s1=pivot - Decimal("0.382") * span,
        s2=pivot - Decimal("0.618") * span,
        s3=pivot - span,
    )


@dataclass(frozen=True)
class Swing:
    index: int
    price: Decimal
    kind: str  # "high" or "low"


@register_indicator("swing_points", min_lookback=lambda strength=2: strength * 2 + 1, category="levels")
def swing_points(bars: Sequence[Bar], strength: int = 2) -> list[Swing]:
    """Fractal swing highs and lows.

    A swing high has ``strength`` lower highs on each side. Confirmation
    therefore lags by ``strength`` bars — which is honest: a swing high is not
    knowable at the time it forms, and pretending otherwise is look-ahead bias.
    """
    if strength < 1:
        raise ValidationError("strength must be at least 1")
    require_history(bars, strength * 2 + 1, indicator=f"swing_points({strength})")

    swings: list[Swing] = []
    for index in range(strength, len(bars) - strength):
        window_before = bars[index - strength : index]
        window_after = bars[index + 1 : index + 1 + strength]
        current = bars[index]

        if all(bar.high < current.high for bar in window_before) and all(
            bar.high < current.high for bar in window_after
        ):
            swings.append(Swing(index=index, price=current.high, kind="high"))
        elif all(bar.low > current.low for bar in window_before) and all(
            bar.low > current.low for bar in window_after
        ):
            swings.append(Swing(index=index, price=current.low, kind="low"))
    return swings


def nearest_levels(
    price: Decimal, levels: Sequence[Decimal], *, count: int = 2
) -> tuple[list[Decimal], list[Decimal]]:
    """``(supports, resistances)`` nearest to ``price``, nearest first."""
    supports = sorted((level for level in levels if level < price), reverse=True)[:count]
    resistances = sorted(level for level in levels if level > price)[:count]
    return supports, resistances
