"""LiveMarketDataProvider — MD-001, MD-002, MD-007, MD-010.

The live implementation of :class:`MarketDataProvider`. It composes the Groww
REST endpoints, the websocket feed, the quote cache, validation and staleness
into the single interface the rest of the system uses.

Two behaviours worth reading before relying on it:

* **Quality gate.** A quote that fails validation is not returned as if it were
  fine. It raises, and the caller treats it exactly as missing data — because a
  crossed book or an out-of-circuit print is not a price you can trade on.
* **Degraded mode.** If the feed drops, the provider keeps serving prices by REST
  polling and marks itself ``DEGRADED``. It never silently substitutes a stale
  cached price for a live one: the cache TTL is short and staleness is still
  checked at the point of use.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Sequence

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.feed import FeedPriority, GrowwFeedClient, Subscription
from app.brokers.groww.historical import GrowwHistoricalApi
from app.brokers.groww.marketdata import GrowwMarketDataApi
from app.brokers.groww.options import GrowwOptionsApi
from app.config import Settings, get_settings
from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, HealthStatus, Segment
from app.core.errors import DataQualityError
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.marketdata.base import MarketDataProvider, TickCallback
from app.marketdata.cache import MemoryQuoteCache, QuoteCache
from app.marketdata.models import (
    Bar,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    OptionChain,
    Quote,
)
from app.marketdata.staleness import FreshnessPolicy, check_fresh, is_fresh
from app.marketdata.subscriptions import Interest, SubscriptionManager
from app.marketdata.validation import QualityCounters, validate_quote

logger = get_logger("marketdata.live")

__all__ = ["LiveMarketDataProvider", "INDEX_SYMBOLS"]

#: Index symbols the system tracks (MD-007).
INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "SENSEX": "SENSEX",
}


class LiveMarketDataProvider(MarketDataProvider):
    name = "live"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        client: Optional[GrowwClient] = None,
        market_api: Optional[GrowwMarketDataApi] = None,
        historical_api: Optional[GrowwHistoricalApi] = None,
        options_api: Optional[GrowwOptionsApi] = None,
        feed: Optional[GrowwFeedClient] = None,
        cache: Optional[QuoteCache] = None,
        clock: Optional[Clock] = None,
        event_bus: Optional[EventBus] = None,
        freshness: Optional[FreshnessPolicy] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._clock = clock or get_clock()
        self._client = client or GrowwClient(self._settings, clock=self._clock)
        self._market = market_api or GrowwMarketDataApi(self._client, clock=self._clock)
        self._historical = historical_api or GrowwHistoricalApi(self._client, clock=self._clock)
        self._options = options_api or GrowwOptionsApi(self._client, clock=self._clock)
        self._cache = cache or MemoryQuoteCache(clock=self._clock)
        self._feed = feed
        self._bus = event_bus
        self._freshness = freshness or FreshnessPolicy.from_settings(self._settings)

        self.subscriptions = SubscriptionManager(feed=feed)
        self.quality = QualityCounters()
        self._tick_callbacks: list[TickCallback] = []
        self._last_seen: dict[str, datetime] = {}
        self._last_price: dict[str, Decimal] = {}
        self._status = HealthStatus.PASS
        self._connected = False
        self._cache_ttl = 1.0

    # --- Identity ---------------------------------------------------------

    @property
    def data_origin(self) -> DataOrigin:
        return DataOrigin.LIVE

    @property
    def status(self) -> HealthStatus:
        """``DEGRADED`` while running on REST polling instead of the feed."""
        if not self._connected or not self._last_seen:
            return HealthStatus.SKIPPED
        if not all(
            is_fresh(observed, kind="tick", policy=self._freshness, clock=self._clock)
            for observed in self._last_seen.values()
        ):
            return HealthStatus.FAIL
        return self._status

    # --- Lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        self._connected = False
        self._status = HealthStatus.DEGRADED
        if self._feed is not None:
            self._feed.on_message(self._on_feed_message)
            try:
                await self._feed.start()
                self.subscriptions.attach(self._feed)
                self._status = HealthStatus.PASS
            except Exception as exc:  # noqa: BLE001 - degrade, do not fail closed here
                # MD-010: no feed is a degradation, not an outage. REST polling
                # still serves prices, at a lower rate.
                self._status = HealthStatus.DEGRADED
                logger.warning(
                    "Market-data feed unavailable; falling back to REST polling",
                    extra={"error": str(exc)},
                )
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        if self._feed is not None:
            await self._feed.stop()
        await self._client.aclose()

    # --- Snapshots --------------------------------------------------------

    async def get_quote(self, instrument: InstrumentRef) -> Quote:
        quote = await self._market.quote(instrument)
        result = validate_quote(quote, previous_price=self._last_price.get(instrument.key))
        self.quality.record(result)

        if not result.ok:
            await self._publish_quality_event(instrument, result)
            raise DataQualityError(
                f"Quote for {instrument.trading_symbol} failed validation: "
                f"{'; '.join(result.details)}",
                context=result.to_dict(),
            )

        self._remember(instrument, quote.ltp, quote.observed_at)
        await self._cache.set(
            LTPQuote(
                instrument=instrument,
                ltp=quote.ltp,
                observed_at=quote.observed_at,
                data_origin=DataOrigin.LIVE,
            ),
            self._cache_ttl,
        )
        return quote

    async def get_ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        """Batch LTP, served from cache where possible."""
        results: dict[str, LTPQuote] = {}
        misses: list[InstrumentRef] = []

        for instrument in instruments:
            cached = await self._cache.get(instrument.key)
            if cached is not None:
                results[instrument.key] = cached
            else:
                misses.append(instrument)

        if misses:
            fetched = await self._market.ltp(misses)
            for key, quote in fetched.items():
                results[key] = quote
                self._remember(quote.instrument, quote.ltp, quote.observed_at)
                await self._cache.set(quote, self._cache_ttl)

        return results

    async def get_ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        return await self._market.ohlc(instruments)

    async def get_index_value(self, name: str) -> IndexValue:
        symbol = INDEX_SYMBOLS.get(name.upper(), name)
        exchange = Exchange.BSE if symbol == "SENSEX" else Exchange.NSE
        return await self._market.index_value(symbol, exchange)

    async def get_index_values(self, names: Sequence[str] = ()) -> dict[str, IndexValue]:
        """All tracked indices at once (MD-007)."""
        wanted = list(names) or list(INDEX_SYMBOLS)
        values: dict[str, IndexValue] = {}
        for name in wanted:
            try:
                values[name] = await self.get_index_value(name)
            except Exception as exc:  # noqa: BLE001 - one index must not break the rest
                logger.warning(
                    "Index value unavailable", extra={"index": name, "error": str(exc)}
                )
        return values

    # --- History ----------------------------------------------------------

    async def get_candles(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        return await self._historical.candles(instrument, interval_minutes, start, end)

    # --- Derivatives ------------------------------------------------------

    async def get_option_chain(
        self, underlying: str, expiry: Optional[date] = None
    ) -> OptionChain:
        return await self._options.chain(underlying, expiry)

    # --- Streaming --------------------------------------------------------

    async def subscribe(
        self,
        instruments: Sequence[InstrumentRef],
        *,
        source: str = "strategy",
        kind: str = "ltp",
    ) -> None:
        await self.subscriptions.register(
            Interest(instrument=instrument, source=source, kind=kind)
            for instrument in instruments
        )

    async def unsubscribe(
        self,
        instruments: Sequence[InstrumentRef],
        *,
        source: str = "strategy",
        kind: str = "ltp",
    ) -> None:
        await self.subscriptions.release(
            Interest(instrument=instrument, source=source, kind=kind)
            for instrument in instruments
        )

    def on_tick(self, callback: TickCallback) -> None:
        self._tick_callbacks.append(callback)

    async def _on_feed_message(self, message: dict) -> None:
        """Normalise a feed message into a tick and fan it out."""
        quote = self._parse_feed_tick(message)
        if quote is None:
            return

        self._remember(quote.instrument, quote.ltp, quote.observed_at)
        await self._cache.set(quote, self._cache_ttl)

        for callback in self._tick_callbacks:
            try:
                await callback(quote)
            except Exception as exc:  # noqa: BLE001 - one consumer must not stop the feed
                logger.error("Tick callback failed", extra={"error": str(exc)})

    def _parse_feed_tick(self, message: dict) -> Optional[LTPQuote]:
        payload = message.get("message") if isinstance(message, dict) else None
        metadata = message.get("metadata") if isinstance(message, dict) else None
        if not isinstance(payload, dict):
            return None

        symbol = payload.get("trading_symbol") or (
            metadata.get("trading_symbol") if isinstance(metadata, dict) else None
        )
        price = payload.get("ltp", payload.get("last_price"))
        if not symbol or price is None:
            return None

        exchange_text = (
            payload.get("exchange")
            or (metadata.get("exchange") if isinstance(metadata, dict) else None)
            or Exchange.NSE.value
        )
        segment_text = (
            payload.get("segment")
            or (metadata.get("segment") if isinstance(metadata, dict) else None)
            or Segment.CASH.value
        )

        try:
            parsed_price = Decimal(str(price))
            if not parsed_price.is_finite() or parsed_price <= 0:
                return None
            instrument = InstrumentRef(
                trading_symbol=str(symbol),
                exchange=Exchange(str(exchange_text).upper()),
                segment=Segment(str(segment_text).upper()),
            )
            return LTPQuote(
                instrument=instrument,
                ltp=parsed_price,
                observed_at=self._clock.now(),
                data_origin=DataOrigin.LIVE,
            )
        except Exception as exc:  # noqa: BLE001 - malformed tick, reported not raised
            logger.warning("Undecodable feed tick", extra={"error": str(exc)})
            return None

    # --- Freshness --------------------------------------------------------

    async def last_update_at(self, instrument: InstrumentRef) -> Optional[datetime]:
        return self._last_seen.get(instrument.key)

    async def require_fresh(self, instrument: InstrumentRef) -> None:
        """Raise unless this instrument has produced data recently (MD-008)."""
        check_fresh(
            self._last_seen.get(instrument.key),
            kind="tick",
            what=f"market data for {instrument.trading_symbol}",
            policy=self._freshness,
            clock=self._clock,
        )

    # --- Internals --------------------------------------------------------

    def _remember(
        self, instrument: InstrumentRef, price: Decimal, observed_at: datetime
    ) -> None:
        self._last_seen[instrument.key] = observed_at
        self._last_price[instrument.key] = price

    async def _publish_quality_event(self, instrument: InstrumentRef, result) -> None:  # type: ignore[no-untyped-def]
        if self._bus is None:
            return
        await self._bus.publish(
            Event(
                type=EventType.DATA_QUALITY,
                payload={
                    "instrument": instrument.key,
                    "anomalies": [a.value for a in result.anomalies],
                    "details": result.details,
                },
            )
        )


def _build(settings: Settings) -> MarketDataProvider:
    return LiveMarketDataProvider(settings)


def register() -> None:
    from app.marketdata.factory import MarketDataProviderName, register_market_data_provider

    register_market_data_provider(MarketDataProviderName.LIVE, _build)


register()
