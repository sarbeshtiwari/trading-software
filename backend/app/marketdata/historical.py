"""HistoricalMarketDataProvider — HD-001.

Reads from the local candle store first and falls through to the broker only for
what is missing, storing whatever it fetches. Two reasons this order matters:

* the broker's intraday history reaches back three months, while the local
  archive keeps growing past that (HD-010);
* a backtest that re-reads the same window a hundred times must not make a
  hundred API calls.

Live snapshot methods are unsupported here on purpose. A historical provider that
answers "what is the price now" invites exactly the mistake ARCH-008 exists to
prevent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Sequence

from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.instruments.resolver import InstrumentResolver, get_resolver
from app.marketdata.backfill import Backfiller
from app.marketdata.base import MarketDataProvider, TickCallback
from app.marketdata.ingest import CandleStore
from app.marketdata.models import (
    Bar,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    OptionChain,
    Quote,
)

logger = get_logger("marketdata.historical")

__all__ = ["HistoricalMarketDataProvider"]


class HistoricalMarketDataProvider(MarketDataProvider):
    name = "historical"

    def __init__(
        self,
        *,
        store: Optional[CandleStore] = None,
        fetch: Optional[object] = None,
        resolver: Optional[InstrumentResolver] = None,
        clock: Optional[Clock] = None,
        auto_backfill: bool = True,
    ) -> None:
        self._clock = clock or get_clock()
        self._store = store or CandleStore(clock=self._clock)
        #: ``async (instrument, interval, start, end) -> Sequence[Bar]``; when
        #: absent the provider is store-only, which is what a backtest wants.
        self._fetch = fetch
        self._resolver = resolver or get_resolver()
        self._auto_backfill = auto_backfill and fetch is not None
        self._backfiller = (
            Backfiller(fetch, store=self._store, clock=self._clock) if fetch else None
        )

    @property
    def data_origin(self) -> DataOrigin:
        return DataOrigin.HISTORICAL

    async def connect(self) -> None:
        if not self._resolver.loaded:
            await self._resolver.refresh()

    async def close(self) -> None:
        return None

    # --- History ----------------------------------------------------------

    async def get_candles(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        instrument_id = self._instrument_id(instrument)

        if self._auto_backfill and self._backfiller is not None:
            await self._backfiller.backfill(
                instrument_id, instrument, interval_minutes, start, end
            )

        bars = await self._store.read(instrument_id, interval_minutes, start, end)

        if not bars and self._fetch is not None and not self._auto_backfill:
            fetched = await self._fetch(instrument, interval_minutes, start, end)  # type: ignore[operator]
            if fetched:
                await self._store.write(instrument_id, interval_minutes, fetched)
                bars = list(fetched)

        return bars

    async def last_n_bars(
        self, instrument: InstrumentRef, interval_minutes: int, count: int
    ) -> list[Bar]:
        return await self._store.last_n(
            self._instrument_id(instrument), interval_minutes, count
        )

    async def coverage(
        self, instrument: InstrumentRef, interval_minutes: int
    ) -> tuple[Optional[datetime], Optional[datetime], int]:
        return await self._store.coverage(
            self._instrument_id(instrument), interval_minutes
        )

    # --- Unsupported by design -------------------------------------------

    async def get_quote(self, instrument: InstrumentRef) -> Quote:
        raise ValidationError(
            "The historical provider has no live quote. Use the live provider, or "
            "read the candle at the timestamp you actually mean."
        )

    async def get_ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        raise ValidationError("The historical provider has no live prices.")

    async def get_ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        raise ValidationError("The historical provider has no live OHLC.")

    async def get_index_value(self, name: str) -> IndexValue:
        raise ValidationError("The historical provider has no live index value.")

    async def get_option_chain(
        self, underlying: str, expiry: Optional[date] = None
    ) -> OptionChain:
        raise ValidationError(
            "Historical option chains come from stored snapshots; use the snapshot "
            "reader rather than the live chain endpoint."
        )

    async def subscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        return None

    async def unsubscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        return None

    def on_tick(self, callback: TickCallback) -> None:
        return None

    async def last_update_at(self, instrument: InstrumentRef) -> Optional[datetime]:
        _, latest, _ = await self.coverage(instrument, 1)
        return latest

    # --- Internals --------------------------------------------------------

    def _instrument_id(self, instrument: InstrumentRef) -> str:
        resolved = self._resolver.get(
            instrument.trading_symbol, instrument.exchange, instrument.segment
        )
        if resolved is not None:
            return resolved.id
        # Fall back to the natural key so a store-only provider still works
        # before the instrument master has been loaded (tests, backtests).
        return instrument.key


def _build(settings) -> MarketDataProvider:  # type: ignore[no-untyped-def]
    return HistoricalMarketDataProvider()


def register() -> None:
    from app.marketdata.factory import MarketDataProviderName, register_market_data_provider

    register_market_data_provider(MarketDataProviderName.HISTORICAL, _build)


register()
