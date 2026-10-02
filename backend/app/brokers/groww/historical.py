"""Groww historical candles — GRW-018, and the constraint behind HD-010.

Two documented limits shape this module:

* **Maximum range per request**, which differs per interval:

  ======  ==========
  1 min   7 days
  5 min   15 days
  10 min  30 days
  60 min  150 days
  240 min 365 days
  1 day   1080 days
  1 week  no limit
  ======  ==========

* **Historical depth**: intraday intervals reach back only **3 months**; daily and
  weekly have full history.

A caller asking for six months of 5-minute bars is not making a mistake — it is
asking for something that takes 12 requests and will still be truncated by the
three-month wall. So the request is split automatically, the windows are stitched
without gaps or duplicates, and the depth limit is reported rather than silently
returning a short series that looks complete.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.mapping import pick
from app.brokers.groww.ratelimit import RateCategory
from app.core.clock import Clock, get_clock, to_ist
from app.core.errors import InvalidResponseError, ValidationError
from app.core.logging import get_logger
from app.marketdata.models import Bar, InstrumentRef

logger = get_logger("brokers.groww.historical")

__all__ = [
    "GrowwHistoricalApi",
    "MAX_RANGE_DAYS",
    "INTRADAY_HISTORY_DAYS",
    "split_window",
    "supported_intervals",
]

#: interval minutes -> maximum days per request. ``None`` means no documented cap.
MAX_RANGE_DAYS: dict[int, Optional[int]] = {
    1: 7,
    5: 15,
    10: 30,
    60: 150,
    240: 365,
    1440: 1080,
    10080: None,
}

#: Intraday history available from Groww, in days (documented as 3 months).
INTRADAY_HISTORY_DAYS = 90

#: Intervals with full history rather than the 3-month intraday wall.
_FULL_HISTORY_INTERVALS = {1440, 10080}


def supported_intervals() -> tuple[int, ...]:
    return tuple(sorted(MAX_RANGE_DAYS))


def split_window(
    start: datetime, end: datetime, interval_minutes: int
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end]`` into windows the API will accept.

    Windows are half-open in effect: each subsequent window begins one second
    after the previous one ends, so stitched results contain no duplicate bar.
    """
    if interval_minutes not in MAX_RANGE_DAYS:
        raise ValidationError(
            f"interval_minutes={interval_minutes} is not supported by Groww "
            f"(supported: {list(supported_intervals())})",
            context={"interval_minutes": interval_minutes},
        )
    if end < start:
        raise ValidationError(
            "end must not be before start",
            context={"start": start.isoformat(), "end": end.isoformat()},
        )

    max_days = MAX_RANGE_DAYS[interval_minutes]
    if max_days is None:
        return [(start, end)]

    span = timedelta(days=max_days)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + span, end)
        windows.append((cursor, window_end))
        if window_end >= end:
            break
        cursor = window_end + timedelta(seconds=1)
    return windows


class GrowwHistoricalApi:
    def __init__(self, client: GrowwClient, *, clock: Optional[Clock] = None) -> None:
        self._client = client
        self._clock = clock or get_clock()

    def earliest_available(self, interval_minutes: int) -> Optional[datetime]:
        """Oldest timestamp Groww will serve for this interval."""
        if interval_minutes in _FULL_HISTORY_INTERVALS:
            return None
        return self._clock.now() - timedelta(days=INTRADAY_HISTORY_DAYS)

    async def candles(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Fetch candles across as many windows as the limits require."""
        windows = split_window(start, end, interval_minutes)

        floor = self.earliest_available(interval_minutes)
        if floor is not None and start < floor:
            # Loud, because a truncated series silently produces a backtest that
            # looks like it covered a period it never saw (BT-014).
            logger.warning(
                "Requested history predates the Groww intraday limit; the series will "
                "start later than requested",
                extra={
                    "trading_symbol": instrument.trading_symbol,
                    "interval_minutes": interval_minutes,
                    "requested_start": start.isoformat(),
                    "earliest_available": floor.isoformat(),
                    "limit_days": INTRADAY_HISTORY_DAYS,
                },
            )

        by_timestamp: dict[datetime, Bar] = {}
        for window_start, window_end in windows:
            for bar in await self._fetch_window(
                instrument, interval_minutes, window_start, window_end
            ):
                # Later windows win on overlap, but timestamps are unique keys so
                # stitching can never duplicate a bar.
                by_timestamp[bar.ts] = bar

        bars = [by_timestamp[ts] for ts in sorted(by_timestamp)]
        logger.info(
            "Fetched historical candles",
            extra={
                "trading_symbol": instrument.trading_symbol,
                "interval_minutes": interval_minutes,
                "windows": len(windows),
                "bars": len(bars),
                "first": bars[0].ts.isoformat() if bars else None,
                "last": bars[-1].ts.isoformat() if bars else None,
            },
        )
        return bars

    async def _fetch_window(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        payload = await self._client.get(
            Endpoints.HISTORICAL_CANDLES.resolve(),
            category=RateCategory.LIVE_DATA,
            params={
                "exchange": instrument.exchange.value,
                "segment": instrument.segment.value,
                "trading_symbol": instrument.trading_symbol,
                "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                "interval_in_minutes": interval_minutes,
            },
        )
        return _parse_candles(payload)


def _parse_candles(payload: Any) -> list[Bar]:
    """Parse the documented ``[ts, open, high, low, close, volume]`` rows."""
    rows: Any
    if isinstance(payload, Mapping):
        rows = pick(payload, "candles", "candle_list", default=[])
    else:
        rows = payload

    if not isinstance(rows, list):
        raise InvalidResponseError(
            "Groww historical payload carried no candle list",
            context={"type": type(payload).__name__},
        )

    bars: list[Bar] = []
    for row in rows:
        bar = _parse_candle_row(row)
        if bar is not None:
            bars.append(bar)
    return bars


def _parse_candle_row(row: Any) -> Optional[Bar]:
    if isinstance(row, Mapping):
        values: Sequence[Any] = (
            pick(row, "timestamp", "ts", "time"),
            pick(row, "open"),
            pick(row, "high"),
            pick(row, "low"),
            pick(row, "close"),
            pick(row, "volume", default=0),
            pick(row, "open_interest", "oi"),
        )
    elif isinstance(row, (list, tuple)):
        values = tuple(row) + (None,) * (7 - len(row))
    else:
        return None

    timestamp, open_, high, low, close, volume, open_interest = values[:7]
    if timestamp is None or None in (open_, high, low, close):
        return None

    ts = _to_datetime(timestamp)
    if ts is None:
        return None

    bar = Bar(
        ts=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=int(volume or 0),
        open_interest=int(open_interest) if open_interest not in (None, "") else None,
    )
    if not bar.is_valid:
        raise InvalidResponseError(
            f"Groww returned an inconsistent candle at {ts.isoformat()}",
            context={
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
            },
        )
    return bar


def _to_datetime(value: Any) -> Optional[datetime]:
    """Candle timestamps are documented as epoch seconds."""
    from datetime import timezone

    if isinstance(value, datetime):
        return to_ist(value)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return to_ist(datetime.fromtimestamp(seconds, tz=timezone.utc))
    text = str(value).strip()
    if text.isdigit():
        return _to_datetime(int(text))
    try:
        return to_ist(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None
