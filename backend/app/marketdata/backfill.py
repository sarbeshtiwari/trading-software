"""Gap detection and backfill — HD-003, HD-004.

A gap is a bar we should have and do not. Deciding *should* is the whole problem:
weekends, holidays, pre-open and post-close minutes all look like missing data to
a naive diff, and chasing them would burn the rate limit forever while never
succeeding.

So the expected timeline is generated from the trading calendar and the session
windows, and only genuinely missing bars are fetched. A gap that persists after a
fetch is reported rather than retried indefinitely — repeated absence usually
means the instrument did not trade, not that the request failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.core.calendar import TradingCalendar, get_trading_calendar
from app.core.clock import Clock, ensure_ist, get_clock
from app.core.enums import Segment
from app.core.logging import get_logger
from app.core.sessions import session_bounds
from app.marketdata.ingest import CandleStore
from app.marketdata.models import Bar, InstrumentRef

logger = get_logger("marketdata.backfill")

__all__ = ["GapReport", "expected_timestamps", "find_gaps", "Backfiller"]


@dataclass
class GapReport:
    instrument: str
    interval_minutes: int
    expected: int = 0
    present: int = 0
    missing: list[datetime] = field(default_factory=list)

    @property
    def gap_count(self) -> int:
        return len(self.missing)

    @property
    def completeness(self) -> float:
        return self.present / self.expected if self.expected else 1.0

    def ranges(self, *, max_ranges: int = 20) -> list[tuple[datetime, datetime]]:
        """Collapse missing timestamps into contiguous fetchable ranges."""
        if not self.missing:
            return []
        ordered = sorted(self.missing)
        step = timedelta(minutes=self.interval_minutes)
        ranges: list[list[datetime]] = [[ordered[0], ordered[0]]]
        for moment in ordered[1:]:
            if moment - ranges[-1][1] <= step:
                ranges[-1][1] = moment
            else:
                ranges.append([moment, moment])
        return [(start, end + step) for start, end in ranges[:max_ranges]]

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "interval_minutes": self.interval_minutes,
            "expected": self.expected,
            "present": self.present,
            "missing": self.gap_count,
            "completeness": round(self.completeness, 4),
        }


def expected_timestamps(
    start: datetime,
    end: datetime,
    interval_minutes: int,
    *,
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
) -> list[datetime]:
    """Bar timestamps that *should* exist between ``start`` and ``end``.

    Only session minutes on trading days. Daily bars get one timestamp per
    trading day, anchored at the session open.
    """
    cal = calendar or get_trading_calendar()
    begin = ensure_ist(start)
    finish = ensure_ist(end)
    if finish < begin:
        return []

    timestamps: list[datetime] = []
    day = begin.date()
    while day <= finish.date():
        bounds = session_bounds(day, segment=segment, calendar=cal)
        if bounds is None:
            day += timedelta(days=1)
            continue

        session_start, session_end = bounds
        if interval_minutes >= 1440:
            if begin <= session_start <= finish:
                timestamps.append(session_start)
            day += timedelta(days=1)
            continue

        cursor = session_start
        step = timedelta(minutes=interval_minutes)
        while cursor < session_end:
            if begin <= cursor <= finish:
                timestamps.append(cursor)
            cursor += step
        day += timedelta(days=1)

    return timestamps


def find_gaps(
    present: set[datetime],
    start: datetime,
    end: datetime,
    interval_minutes: int,
    *,
    instrument: str = "",
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
) -> GapReport:
    expected = expected_timestamps(
        start, end, interval_minutes, segment=segment, calendar=calendar
    )
    normalised = {ensure_ist(ts) for ts in present}
    missing = [ts for ts in expected if ts not in normalised]

    return GapReport(
        instrument=instrument,
        interval_minutes=interval_minutes,
        expected=len(expected),
        present=len(expected) - len(missing),
        missing=missing,
    )


class Backfiller:
    """Finds gaps and fetches only those, never the whole window again."""

    def __init__(
        self,
        fetch: object,
        *,
        store: Optional[CandleStore] = None,
        calendar: Optional[TradingCalendar] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        #: ``async (instrument, interval, start, end) -> Sequence[Bar]``
        self._fetch = fetch
        self._store = store or CandleStore(clock=clock)
        self._calendar = calendar or get_trading_calendar()
        self._clock = clock or get_clock()

    async def report(
        self,
        instrument_id: str,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> GapReport:
        present = await self._store.existing_timestamps(
            instrument_id, interval_minutes, start, end
        )
        return find_gaps(
            present,
            start,
            end,
            interval_minutes,
            instrument=instrument.key,
            segment=instrument.segment,
            calendar=self._calendar,
        )

    async def backfill(
        self,
        instrument_id: str,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
        *,
        max_ranges: int = 20,
    ) -> GapReport:
        """Fetch and store only the missing ranges. Returns the post-fill report."""
        before = await self.report(instrument_id, instrument, interval_minutes, start, end)
        if not before.missing:
            return before

        ranges = before.ranges(max_ranges=max_ranges)
        logger.info(
            "Backfilling candle gaps",
            extra={
                "instrument": instrument.key,
                "interval_minutes": interval_minutes,
                "missing": before.gap_count,
                "ranges": len(ranges),
            },
        )

        for range_start, range_end in ranges:
            bars: Sequence[Bar] = await self._fetch(  # type: ignore[operator]
                instrument, interval_minutes, range_start, range_end
            )
            if bars:
                await self._store.write(instrument_id, interval_minutes, bars)

        after = await self.report(instrument_id, instrument, interval_minutes, start, end)
        if after.missing:
            # Not retried forever: an instrument that did not trade has no bars,
            # and hammering the API cannot conjure them.
            logger.info(
                "Gaps remain after backfill; the instrument likely did not trade then",
                extra={
                    "instrument": instrument.key,
                    "remaining": after.gap_count,
                    "completeness": round(after.completeness, 4),
                },
            )
        return after
