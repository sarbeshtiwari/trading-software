"""Tick-to-bar aggregation — MD-006.

Builds 1/5/15/60-minute bars from ticks, with boundaries anchored to the IST
session rather than to the wall clock. That anchoring is the whole point: NSE
opens at 09:15, so a 5-minute bar runs 09:15–09:20, not 09:15–09:20 only by luck
of when the process happened to start.

A bar is emitted when a tick arrives *after* its close, never on a timer. Emitting
on a timer would publish a bar that later receives another tick belonging to it,
and every indicator computed from that bar would then be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Callable, Optional, Sequence

from app.core.clock import IST, ensure_ist
from app.core.logging import get_logger
from app.marketdata.models import Bar, InstrumentRef

logger = get_logger("marketdata.aggregator")

__all__ = ["bar_start", "BarBuilder", "CandleAggregator", "SUPPORTED_INTERVALS"]

#: Session anchor. Bars are aligned to this, not to the top of the hour.
SESSION_OPEN = time(9, 15)

SUPPORTED_INTERVALS = (1, 5, 15, 30, 60)


def bar_start(moment: datetime, interval_minutes: int, *, anchor: time = SESSION_OPEN) -> datetime:
    """The opening timestamp of the bar containing ``moment``.

    Anchored to the session open, so a 5-minute bar covers 09:15–09:20 and a
    15-minute bar covers 09:15–09:30. Before the anchor (pre-open activity) the
    bars align backwards from it, which keeps the arithmetic total.
    """
    if interval_minutes <= 0:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")

    when = ensure_ist(moment)
    anchor_dt = datetime.combine(when.date(), anchor, tzinfo=IST)
    elapsed = (when - anchor_dt).total_seconds() / 60.0
    # floor() rather than int(): negative elapsed (pre-open) must round down.
    index = int(elapsed // interval_minutes)
    return anchor_dt + timedelta(minutes=index * interval_minutes)


@dataclass
class BarBuilder:
    """Accumulates ticks into one bar."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    open_interest: Optional[int] = None
    ticks: int = 0

    def update(
        self, price: Decimal, volume_delta: int = 0, open_interest: Optional[int] = None
    ) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += max(0, volume_delta)
        if open_interest is not None:
            self.open_interest = open_interest
        self.ticks += 1

    def to_bar(self) -> Bar:
        return Bar(
            ts=self.ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_interest=self.open_interest,
        )


BarCallback = Callable[[InstrumentRef, int, Bar], None]


class CandleAggregator:
    """Per-instrument, per-interval bar construction from a tick stream."""

    def __init__(
        self,
        intervals: Sequence[int] = (1, 5, 15),
        *,
        on_bar_closed: Optional[BarCallback] = None,
        anchor: time = SESSION_OPEN,
    ) -> None:
        for interval in intervals:
            if interval <= 0:
                raise ValueError(f"interval must be positive, got {interval}")
        self._intervals = tuple(intervals)
        self._anchor = anchor
        self._on_bar_closed = on_bar_closed
        self._builders: dict[tuple[str, int], BarBuilder] = {}
        #: Last cumulative volume seen per instrument, to derive per-tick deltas.
        self._last_volume: dict[str, int] = {}

    @property
    def intervals(self) -> tuple[int, ...]:
        return self._intervals

    def on_tick(
        self,
        instrument: InstrumentRef,
        price: Decimal,
        observed_at: datetime,
        *,
        cumulative_volume: Optional[int] = None,
        open_interest: Optional[int] = None,
    ) -> list[tuple[int, Bar]]:
        """Feed one tick. Returns the bars that closed because of it."""
        when = ensure_ist(observed_at)
        volume_delta = 0
        if cumulative_volume is not None:
            previous = self._last_volume.get(instrument.key)
            # A cumulative counter that goes backwards means a new session.
            volume_delta = (
                cumulative_volume - previous
                if previous is not None and cumulative_volume >= previous
                else cumulative_volume
            )
            self._last_volume[instrument.key] = cumulative_volume

        closed: list[tuple[int, Bar]] = []

        for interval in self._intervals:
            key = (instrument.key, interval)
            start = bar_start(when, interval, anchor=self._anchor)
            builder = self._builders.get(key)

            if builder is None:
                self._builders[key] = BarBuilder(
                    ts=start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=max(0, volume_delta),
                    open_interest=open_interest,
                    ticks=1,
                )
                continue

            if start > builder.ts:
                # The tick belongs to the next bar, so the previous one is final.
                bar = builder.to_bar()
                closed.append((interval, bar))
                if self._on_bar_closed is not None:
                    self._on_bar_closed(instrument, interval, bar)
                self._builders[key] = BarBuilder(
                    ts=start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=max(0, volume_delta),
                    open_interest=open_interest,
                    ticks=1,
                )
            elif start < builder.ts:
                # Out-of-order tick from a bar already closed; dropping it is
                # correct, but it must be visible.
                logger.warning(
                    "Discarding an out-of-order tick",
                    extra={
                        "trading_symbol": instrument.trading_symbol,
                        "tick_ts": when.isoformat(),
                        "current_bar": builder.ts.isoformat(),
                    },
                )
            else:
                builder.update(price, volume_delta, open_interest)

        return closed

    def current(self, instrument: InstrumentRef, interval_minutes: int) -> Optional[Bar]:
        """The bar being built right now (incomplete)."""
        builder = self._builders.get((instrument.key, interval_minutes))
        return builder.to_bar() if builder is not None else None

    def flush(self, instrument: Optional[InstrumentRef] = None) -> list[tuple[int, Bar]]:
        """Close every open bar — used at session end (OMS-007)."""
        closed: list[tuple[int, Bar]] = []
        for key in list(self._builders):
            if instrument is not None and key[0] != instrument.key:
                continue
            builder = self._builders.pop(key)
            closed.append((key[1], builder.to_bar()))
        return closed

    def reset(self) -> None:
        self._builders.clear()
        self._last_volume.clear()
