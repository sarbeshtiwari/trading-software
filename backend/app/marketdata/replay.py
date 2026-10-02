"""Replay provider — HD-008, PAPER-008, BT-002.

Feeds recorded bars at their nominal closing times. Unclosed bars are withheld
and quotes retain their original availability timestamps. Session-shortened bars
and publication delays require a separate availability model.

The replay clock is the injected clock. Advancing the cursor advances time, so
every time-dependent behaviour (session phase, staleness, square-off cutoff)
behaves as it would have on the day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.clock import FakeClock, ensure_ist
from app.core.data_origin import DataOrigin
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.marketdata.base import MarketDataProvider, TickCallback
from app.marketdata.factory import MarketDataProviderName, register_market_data_provider
from app.marketdata.models import (
    Bar,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    OptionChain,
    Quote,
)
from app.marketdata.recorded import RecordedSnapshot, SnapshotTape

logger = get_logger("marketdata.replay")

__all__ = ["ReplayMarketDataProvider", "ReplaySeries"]


@dataclass
class ReplaySeries:
    """One instrument/interval series, with a cursor."""

    instrument: InstrumentRef
    interval_minutes: int
    bars: list[Bar]
    cursor: int = 0

    def __post_init__(self) -> None:
        if type(self.interval_minutes) is not int or self.interval_minutes <= 0:
            raise ValidationError("Replay interval must be a positive integer")
        for bar in self.bars:
            if (
                not isinstance(bar.ts, datetime)
                or bar.ts.utcoffset() is None
                or any(
                    not isinstance(price, Decimal) or not price.is_finite() or price <= 0
                    for price in (bar.open, bar.high, bar.low, bar.close)
                )
                or not bar.is_valid
                or type(bar.volume) is not int
                or bar.volume < 0
                or (
                    bar.open_interest is not None
                    and (type(bar.open_interest) is not int or bar.open_interest < 0)
                )
            ):
                raise ValidationError("Invalid or unzoned replay candle")
        self.bars = sorted(self.bars, key=lambda bar: ensure_ist(bar.ts))
        if any(
            current.ts - previous.ts < timedelta(minutes=self.interval_minutes)
            for previous, current in zip(self.bars, self.bars[1:], strict=False)
        ):
            raise ValidationError("Duplicate or overlapping replay candles")

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self.bars)

    @property
    def next_ts(self) -> datetime | None:
        return (
            ensure_ist(self.bars[self.cursor].ts) + timedelta(minutes=self.interval_minutes)
            if not self.exhausted
            else None
        )

    def visible(self) -> list[Bar]:
        return self.bars[: self.cursor]

    def advance(self) -> Bar | None:
        if self.exhausted:
            return None
        bar = self.bars[self.cursor]
        self.cursor += 1
        return bar


class ReplayMarketDataProvider(MarketDataProvider):
    """Deterministic replay of recorded bars."""

    name = "replay"

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._series: dict[str, ReplaySeries] = {}
        self._clock = clock or FakeClock()
        self._tick_callbacks: list[TickCallback] = []
        self._last_bar: dict[str, Bar] = {}
        self._last_observed: dict[str, datetime] = {}
        self._last_step: datetime | None = None
        self._stepping = False
        self._failed = False
        self._snapshots = SnapshotTape()
        self.recording_sha256 = None

    @property
    def data_origin(self) -> DataOrigin:
        # Never LIVE: ARCH-008 uses this to keep replayed data out of execution.
        return DataOrigin.REPLAY

    @property
    def clock(self) -> FakeClock:
        return self._clock

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # --- Loading ----------------------------------------------------------

    def load(self, instrument: InstrumentRef, interval_minutes: int, bars: Sequence[Bar]) -> None:
        if self._last_step is not None:
            raise ValidationError("Replay inputs cannot change after stepping starts")
        key = f"{instrument.key}:{interval_minutes}"
        self._series[key] = ReplaySeries(
            instrument=instrument, interval_minutes=interval_minutes, bars=list(bars)
        )
        logger.info(
            "Loaded replay series",
            extra={
                "instrument": instrument.key,
                "interval_minutes": interval_minutes,
                "bars": len(bars),
            },
        )

    @property
    def series(self) -> list[ReplaySeries]:
        return list(self._series.values())

    @property
    def exhausted(self) -> bool:
        return (
            all(series.exhausted for series in self._series.values())
            and self._snapshots.next_at is None
        )

    def load_snapshots(self, snapshots: Sequence[RecordedSnapshot]) -> None:
        if self._last_step is not None:
            raise ValidationError("Replay inputs cannot change after stepping starts")
        self._snapshots.load(snapshots)

    def recording_evidence(self):
        return [
            {
                "identity": snapshot.key,
                "source": snapshot.source,
                "available_at": snapshot.available_at,
                "observed_at": snapshot.value.observed_at,
                "original_data_origin": snapshot.value.data_origin,
                "recording_sha256": self.recording_sha256,
            }
            for snapshot in self._snapshots.visible.values()
            if snapshot.available_at <= self._clock.now()
        ]

    # --- Stepping ---------------------------------------------------------

    @property
    def next_event_at(self):
        moments = [series.next_ts for series in self._series.values() if series.next_ts is not None]
        if self._snapshots.next_at is not None:
            moments.append(self._snapshots.next_at)
        return min(moments) if moments else None

    async def prime(self):
        """Publish warm-up evidence without rewinding the application's clock."""
        if self._last_step is not None or self._tick_callbacks or self._stepping or self._failed:
            raise ValidationError("Replay warm-up requires an unused provider without callbacks")
        application_clock = self._clock
        until = application_clock.now()
        self._clock = FakeClock(self.next_event_at or until)
        try:
            while self.next_event_at is not None and self.next_event_at <= until:
                await self.step()
        except BaseException:
            self._failed = True
            raise
        finally:
            self._clock = application_clock
            self._last_step = until

    async def step(self) -> datetime | None:
        """Publish complete bars at their closing instant, before notifying subscribers."""
        if self._stepping or self._failed:
            raise ValidationError("Replay is busy or failed; start a new run after failure")
        self._stepping = True
        try:
            return await self._step()
        finally:
            self._stepping = False

    async def _step(self) -> datetime | None:
        pending = [series for series in self._series.values() if not series.exhausted]
        moments = [series.next_ts for series in pending if series.next_ts is not None]
        if self._snapshots.next_at is not None:
            moments.append(self._snapshots.next_at)
        if not moments:
            return None

        moment = min(moments)
        if self._last_step is not None and (
            self._clock.now() < self._last_step or moment < self._clock.now()
        ):
            raise ValidationError("Replay clock cannot move backwards")
        due = sorted(
            (series for series in pending if series.next_ts == moment),
            key=lambda series: (series.instrument.key, series.interval_minutes),
        )
        snapshots = {}
        for series in due:
            bar = series.bars[series.cursor]
            previous = snapshots.get(series.instrument.key)
            if previous is not None and previous[1].close != bar.close:
                raise ValidationError("Conflicting replay interval closes")
            snapshots.setdefault(series.instrument.key, (series.instrument, bar))
        self._clock.set_to(moment)
        self._last_step = moment
        for series in due:
            series.advance()
        for key, (_instrument, bar) in snapshots.items():
            self._last_bar[key] = bar
            self._last_observed[key] = moment
        recorded_quotes = self._snapshots.publish(moment)
        notifications = {
            instrument.key: LTPQuote(
                instrument=instrument,
                ltp=bar.close,
                observed_at=moment,
                data_origin=DataOrigin.REPLAY,
            )
            for instrument, bar in snapshots.values()
            if not self._snapshots.declared(("quote", instrument.key))
        }
        notifications.update(
            {
                quote.instrument.key: LTPQuote(
                    instrument=quote.instrument,
                    ltp=quote.ltp,
                    observed_at=quote.observed_at,
                    data_origin=DataOrigin.REPLAY,
                )
                for quote in recorded_quotes
            }
        )
        try:
            for notification in notifications.values():
                for callback in self._tick_callbacks:
                    await callback(notification)
        except BaseException:
            self._failed = True
            raise
        return moment

    async def run(self, max_steps: int | None = None) -> int:
        steps = 0
        while (max_steps is None or steps < max_steps) and not self.exhausted:
            if await self.step() is None:
                break
            steps += 1
        return steps

    # --- Reads (cursor-bounded) -------------------------------------------

    async def get_candles(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        """Only bars already replayed, and never past ``end``."""
        series = self._series.get(f"{instrument.key}:{interval_minutes}")
        if series is None:
            return []
        begin = ensure_ist(start)
        finish = ensure_ist(end)
        now = self._clock.now()
        return [
            bar
            for bar in series.visible()
            if begin <= ensure_ist(bar.ts) <= finish
            and ensure_ist(bar.ts) + timedelta(minutes=series.interval_minutes) <= now
        ]

    async def get_ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        results: dict[str, LTPQuote] = {}
        for instrument in instruments:
            if self._snapshots.declared(("quote", instrument.key)):
                quote = self._snapshots.read(("quote", instrument.key), self._clock.now())
                if quote is not None:
                    results[instrument.key] = LTPQuote(
                        instrument=instrument,
                        ltp=quote.ltp,
                        observed_at=quote.observed_at,
                        data_origin=DataOrigin.REPLAY,
                    )
                continue
            bar = self._last_bar.get(instrument.key)
            if bar is None or self._last_observed[instrument.key] > self._clock.now():
                continue
            results[instrument.key] = LTPQuote(
                instrument=instrument,
                ltp=bar.close,
                observed_at=self._last_observed[instrument.key],
                data_origin=DataOrigin.REPLAY,
            )
        return results

    async def get_quote(self, instrument: InstrumentRef) -> Quote:
        if self._snapshots.declared(("quote", instrument.key)):
            quote = self._snapshots.read(("quote", instrument.key), self._clock.now())
            if quote is None:
                raise ValidationError("Recorded quote not yet available")
            return quote
        bar = self._last_bar.get(instrument.key)
        if bar is None or self._last_observed[instrument.key] > self._clock.now():
            raise ValidationError(
                f"No replayed data yet for {instrument.trading_symbol}",
                context={"instrument": instrument.key},
            )
        return Quote(
            instrument=instrument,
            ltp=bar.close,
            observed_at=self._last_observed[instrument.key],
            data_origin=DataOrigin.REPLAY,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            open_interest=bar.open_interest,
        )

    async def get_ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        results: dict[str, OHLCQuote] = {}
        for instrument in instruments:
            if self._snapshots.declared(("ohlc", instrument.key)):
                quote = self._snapshots.read(("ohlc", instrument.key), self._clock.now())
                if quote is not None:
                    results[instrument.key] = quote
                continue
            bar = self._last_bar.get(instrument.key)
            if bar is None or self._last_observed[instrument.key] > self._clock.now():
                continue
            results[instrument.key] = OHLCQuote(
                instrument=instrument,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                observed_at=self._last_observed[instrument.key],
                data_origin=DataOrigin.REPLAY,
            )
        return results

    async def get_index_value(self, name: str) -> IndexValue:
        raise ValidationError("Replay does not serve index values directly.")

    async def get_option_chain(self, underlying: str, expiry: date | None = None) -> OptionChain:
        if expiry is None:
            raise ValidationError("Recorded chain replay requires explicit expiry")
        chain = self._snapshots.read(("chain", underlying, expiry), self._clock.now())
        if chain is None:
            raise ValidationError("Recorded option chain not yet available")
        return chain

    async def subscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        return None

    async def unsubscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        return None

    def on_tick(self, callback: TickCallback) -> None:
        self._tick_callbacks.append(callback)

    async def last_update_at(self, instrument: InstrumentRef) -> datetime | None:
        if self._snapshots.declared(("quote", instrument.key)):
            quote = self._snapshots.read(("quote", instrument.key), self._clock.now())
            return quote.observed_at if quote is not None else None
        observed = self._last_observed.get(instrument.key)
        return observed if observed is not None and observed <= self._clock.now() else None


def _build(settings) -> MarketDataProvider:  # type: ignore[no-untyped-def]
    return ReplayMarketDataProvider()


def register() -> None:
    register_market_data_provider(MarketDataProviderName.REPLAY, _build)


register()
