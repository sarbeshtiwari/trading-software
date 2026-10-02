"""Multi-timeframe alignment — TA-008.

Higher-timeframe context is one of the easiest places in a trading system to
introduce look-ahead bias, and one of the hardest places to notice it. If a 5-minute
strategy reads "the 60-minute RSI" at 09:20, and that value was computed from a
60-minute bar covering 09:15–10:15, then it has read the future: the bar had not
closed yet.

So alignment here is strict. At any moment ``t``, a higher-timeframe value is
visible only if its bar **closed at or before t**. The resampler follows the same
rule as the live aggregator (session-anchored boundaries), so replayed and live
behaviour match.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional, Sequence

from app.analysis.technical.base import Values
from app.core.clock import ensure_ist
from app.core.errors import ValidationError
from app.marketdata.aggregator import bar_start
from app.marketdata.models import Bar

__all__ = ["resample", "align_to_base", "higher_timeframe_value"]


def resample(bars: Sequence[Bar], interval_minutes: int) -> list[Bar]:
    """Build higher-timeframe bars from lower-timeframe ones.

    Only **complete** groups are emitted... except the final one, which is
    emitted too and flagged by having a later-than-expected last constituent.
    Callers that must not see an in-progress bar use :func:`align_to_base`, which
    filters on close time rather than on group membership.
    """
    if interval_minutes <= 0:
        raise ValidationError("interval_minutes must be positive")
    if not bars:
        return []

    grouped: dict = {}
    order: list = []
    for bar in bars:
        start = bar_start(ensure_ist(bar.ts), interval_minutes)
        if start not in grouped:
            grouped[start] = []
            order.append(start)
        grouped[start].append(bar)

    resampled: list[Bar] = []
    for start in order:
        members = grouped[start]
        resampled.append(
            Bar(
                ts=start,
                open=members[0].open,
                high=max(member.high for member in members),
                low=min(member.low for member in members),
                close=members[-1].close,
                volume=sum(member.volume for member in members),
                open_interest=members[-1].open_interest,
            )
        )
    return resampled


def align_to_base(
    base_bars: Sequence[Bar],
    higher_bars: Sequence[Bar],
    higher_values: Values,
    higher_interval_minutes: int,
) -> Values:
    """Project higher-timeframe values onto the base series, without look-ahead.

    ``result[i]`` is the most recent higher-timeframe value whose bar had closed
    by ``base_bars[i].ts``.
    """
    if len(higher_bars) != len(higher_values):
        raise ValidationError(
            f"higher_bars ({len(higher_bars)}) and higher_values "
            f"({len(higher_values)}) must be the same length"
        )

    span = timedelta(minutes=higher_interval_minutes)
    closes_at = [ensure_ist(bar.ts) + span for bar in higher_bars]

    result: Values = [None] * len(base_bars)
    cursor = 0
    latest: Optional[Decimal] = None

    for index, bar in enumerate(base_bars):
        moment = ensure_ist(bar.ts)
        # Advance while the next higher bar has genuinely closed.
        while cursor < len(higher_bars) and closes_at[cursor] <= moment:
            if higher_values[cursor] is not None:
                latest = higher_values[cursor]
            cursor += 1
        result[index] = latest
    return result


def higher_timeframe_value(
    base_bars: Sequence[Bar],
    higher_interval_minutes: int,
    compute,  # Callable[[Sequence[Bar]], Values]
) -> Values:
    """Resample, compute an indicator on the higher timeframe, align it back.

    The convenience path for "give me the 60-minute RSI on my 5-minute chart"
    that cannot accidentally read an unclosed bar.
    """
    higher = resample(base_bars, higher_interval_minutes)
    if not higher:
        return [None] * len(base_bars)
    values = compute(higher)
    return align_to_base(base_bars, higher, values, higher_interval_minutes)
