"""Groww live market data — GRW-017.

Batch limits are the documented ones: **50 instruments per call** for LTP and
OHLC. Callers pass whatever list they have and this module chunks it, because a
per-symbol loop over a 200-name universe would spend the entire live-data budget
(10/s, 300/min) on one pass.

Every returned model carries ``observed_at`` and ``DataOrigin.LIVE`` so staleness
(MD-008) and provenance (ARCH-008) stay decidable downstream.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.mapping import pick
from app.brokers.groww.ratelimit import RateCategory
from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.core.errors import InvalidResponseError
from app.core.logging import get_logger
from app.marketdata.models import (
    DepthLevel,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    Quote,
)

logger = get_logger("brokers.groww.marketdata")

__all__ = ["GrowwMarketDataApi", "MAX_BATCH_SYMBOLS", "chunked"]

#: Documented cap for get_ltp / get_ohlc.
MAX_BATCH_SYMBOLS = 50


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _exchange_symbol(instrument: InstrumentRef) -> str:
    """Groww addresses batch quotes as ``EXCHANGE_SYMBOL`` (e.g. ``NSE_RELIANCE``)."""
    return f"{instrument.exchange.value}_{instrument.trading_symbol}"


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _depth(rows: Any) -> tuple[DepthLevel, ...]:
    if not isinstance(rows, list):
        return ()
    levels: list[DepthLevel] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        price = _decimal(pick(row, "price"))
        quantity = _int(pick(row, "quantity", "qty"))
        if price is None or quantity is None:
            continue
        levels.append(DepthLevel(price=price, quantity=quantity, orders=_int(pick(row, "orders"))))
    return tuple(levels)


class GrowwMarketDataApi:
    def __init__(self, client: GrowwClient, *, clock: Optional[Clock] = None) -> None:
        self._client = client
        self._clock = clock or get_clock()

    # --- Quote ------------------------------------------------------------

    async def quote(self, instrument: InstrumentRef) -> Quote:
        """Full quote: depth ladder, OI, circuit limits, 52-week range."""
        result = await self._client.get(
            Endpoints.QUOTE.resolve(),
            category=RateCategory.LIVE_DATA,
            params={
                "exchange": instrument.exchange.value,
                "segment": instrument.segment.value,
                "trading_symbol": instrument.trading_symbol,
            },
        )
        if not isinstance(result, Mapping):
            raise InvalidResponseError(
                "Groww quote payload was not an object",
                context={"type": type(result).__name__},
            )
        return self._parse_quote(instrument, result)

    def _parse_quote(self, instrument: InstrumentRef, payload: Mapping[str, Any]) -> Quote:
        ltp = _decimal(pick(payload, "last_price", "ltp"))
        if ltp is None:
            raise InvalidResponseError(
                f"Groww quote for {instrument.trading_symbol} carried no last price",
                context={"keys": sorted(payload)},
            )

        ohlc = pick(payload, "ohlc", default={}) or {}
        depth = pick(payload, "depth", default={}) or {}

        return Quote(
            instrument=instrument,
            ltp=ltp,
            observed_at=self._clock.now(),
            data_origin=DataOrigin.LIVE,
            open=_decimal(pick(ohlc, "open") if isinstance(ohlc, Mapping) else None),
            high=_decimal(pick(ohlc, "high") if isinstance(ohlc, Mapping) else None),
            low=_decimal(pick(ohlc, "low") if isinstance(ohlc, Mapping) else None),
            close=_decimal(pick(ohlc, "close") if isinstance(ohlc, Mapping) else None),
            previous_close=_decimal(pick(payload, "previous_close", "prev_close")),
            volume=_int(pick(payload, "volume", "total_traded_volume")),
            average_price=_decimal(pick(payload, "average_price")),
            day_change=_decimal(pick(payload, "day_change")),
            day_change_pct=_decimal(pick(payload, "day_change_perc", "day_change_pct")),
            bids=_depth(pick(depth, "buy") if isinstance(depth, Mapping) else None),
            asks=_depth(pick(depth, "sell") if isinstance(depth, Mapping) else None),
            open_interest=_int(pick(payload, "open_interest", "oi")),
            open_interest_change=_int(
                pick(payload, "open_interest_change", "oi_day_change")
            ),
            implied_volatility=_decimal(pick(payload, "implied_volatility", "iv")),
            upper_circuit=_decimal(pick(payload, "upper_circuit_limit")),
            lower_circuit=_decimal(pick(payload, "lower_circuit_limit")),
            week52_high=_decimal(pick(payload, "week_52_high", "year_high")),
            week52_low=_decimal(pick(payload, "week_52_low", "year_low")),
            market_cap=_decimal(pick(payload, "market_cap")),
            raw=dict(payload),
        )

    # --- Batched snapshots -------------------------------------------------

    async def ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        """Last traded price for many instruments, chunked to the batch limit."""
        return await self._batched(
            instruments,
            endpoint=Endpoints.LTP.resolve(),
            build=self._parse_ltp,
        )

    async def ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        """Session OHLC for many instruments, chunked to the batch limit."""
        return await self._batched(
            instruments,
            endpoint=Endpoints.OHLC.resolve(),
            build=self._parse_ohlc,
        )

    async def _batched(self, instruments, endpoint, build):  # type: ignore[no-untyped-def]
        if not instruments:
            return {}

        by_key = {_exchange_symbol(ref): ref for ref in instruments}
        results: dict[str, Any] = {}
        segments = {ref.segment for ref in instruments}

        for segment in segments:
            refs = [ref for ref in instruments if ref.segment is segment]
            for batch in chunked(refs, MAX_BATCH_SYMBOLS):
                symbols = [_exchange_symbol(ref) for ref in batch]
                payload = await self._client.get(
                    endpoint,
                    category=RateCategory.LIVE_DATA,
                    params={
                        "segment": segment.value,
                        "exchange_symbols": ",".join(symbols),
                    },
                )
                if not isinstance(payload, Mapping):
                    raise InvalidResponseError(
                        "Groww batch quote payload was not an object",
                        context={"type": type(payload).__name__},
                    )
                for key, value in payload.items():
                    ref = by_key.get(key) or _ref_from_key(key, segment)
                    built = build(ref, value)
                    if built is not None:
                        results[ref.key] = built

        missing = [ref.key for ref in instruments if ref.key not in results]
        if missing:
            # Reported, not silently dropped: a strategy acting on a missing
            # price must be able to tell that it is missing.
            logger.warning(
                "Groww returned no data for some instruments",
                extra={"missing": missing[:20], "count": len(missing)},
            )
        return results

    def _parse_ltp(self, instrument: InstrumentRef, value: Any) -> Optional[LTPQuote]:
        price = _decimal(value if not isinstance(value, Mapping) else pick(value, "ltp", "last_price"))
        if price is None:
            return None
        return LTPQuote(
            instrument=instrument,
            ltp=price,
            observed_at=self._clock.now(),
            data_origin=DataOrigin.LIVE,
        )

    def _parse_ohlc(self, instrument: InstrumentRef, value: Any) -> Optional[OHLCQuote]:
        if not isinstance(value, Mapping):
            return None
        open_ = _decimal(pick(value, "open"))
        high = _decimal(pick(value, "high"))
        low = _decimal(pick(value, "low"))
        close = _decimal(pick(value, "close"))
        if None in (open_, high, low, close):
            return None
        return OHLCQuote(
            instrument=instrument,
            open=open_,  # type: ignore[arg-type]
            high=high,  # type: ignore[arg-type]
            low=low,  # type: ignore[arg-type]
            close=close,  # type: ignore[arg-type]
            observed_at=self._clock.now(),
            previous_close=_decimal(pick(value, "previous_close", "prev_close")),
            data_origin=DataOrigin.LIVE,
        )

    # --- Indices ----------------------------------------------------------

    async def index_value(self, name: str, exchange: Exchange = Exchange.NSE) -> IndexValue:
        ref = InstrumentRef(trading_symbol=name, exchange=exchange, segment=Segment.CASH)
        quotes = await self.ltp([ref])
        quote = quotes.get(ref.key)
        if quote is None:
            raise InvalidResponseError(
                f"Groww returned no value for index {name}",
                context={"index": name},
            )
        return IndexValue(
            name=name,
            value=quote.ltp,
            observed_at=quote.observed_at,
            data_origin=DataOrigin.LIVE,
        )


def _ref_from_key(key: str, segment: Segment) -> InstrumentRef:
    """Rebuild a reference from an ``EXCHANGE_SYMBOL`` response key."""
    exchange_name, _, symbol = key.partition("_")
    try:
        exchange = Exchange(exchange_name.upper())
    except ValueError:
        exchange = Exchange.NSE
        symbol = key
    return InstrumentRef(trading_symbol=symbol, exchange=exchange, segment=segment)
