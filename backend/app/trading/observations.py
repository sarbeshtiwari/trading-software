"""Provider-fed, audited closed-candle observations for the reference strategy."""

from dataclasses import asdict, dataclass
from datetime import timedelta

import sqlalchemy as sa

from app.analysis.technical.ma import sma
from app.analysis.technical.volatility import atr
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.marketdata.ingest import CandleStore
from app.marketdata.models import InstrumentRef, Quote
from app.marketdata.validation import validate_bar, validate_quote
from app.modes import TradingMode
from app.strategies.context import ContextValue, validate_context_inputs


@dataclass(frozen=True)
class QuoteObservation:
    chain_id: str
    quote: Quote


class ReferenceWarmupUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceObservation:
    chain_id: str
    available: dict[str, ContextValue]
    quote: Quote


class ReferenceIngestion:
    def __init__(self, provider, *, clock=None, max_quote_age_seconds=15):
        self.provider = provider
        self.clock = clock or get_clock()
        self.max_quote_age = timedelta(seconds=max_quote_age_seconds)
        if self.max_quote_age <= timedelta(0):
            raise ValueError("positive quote age required")

    async def collect(self, instrument_id):
        chain_id = new_id("obs")
        try:
            return await self._collect(instrument_id, chain_id)
        except Exception as error:
            await AuditService(self.clock).append(
                AuditIdentity(
                    chain_id=chain_id,
                    event_type="MARKET_INPUT_REJECTED",
                    actor="reference_ingestion",
                    mode=TradingMode.PAPER,
                ),
                {
                    "instrument_id": instrument_id,
                    "result": {
                        "reason": "REFERENCE_WARMUP_UNAVAILABLE"
                        if isinstance(error, ReferenceWarmupUnavailable)
                        else "INVALID_MARKET_INPUT"
                    },
                },
            )
            raise

    async def _collect(self, instrument_id, chain_id):
        instrument = await self._instrument(instrument_id)
        reference = InstrumentRef(
            instrument.trading_symbol, instrument.exchange, instrument.segment
        )
        requested_at = self.clock.now().replace(second=0, microsecond=0)
        bars = tuple(
            await self.provider.get_candles(
                reference,
                1,
                requested_at - timedelta(minutes=21),
                requested_at - timedelta(minutes=1),
            )
        )
        quote = await self.provider.get_quote(reference)
        as_of = self.validate_quote(quote, reference)
        if len(bars) < 21:
            raise ReferenceWarmupUnavailable("21 closed candles not yet available")
        if len(bars) != 21:
            raise ValueError("21 contiguous closed candles required")
        return await self._store_observation(instrument_id, chain_id, quote, bars, as_of)

    async def observe_quote(self, instrument_id):
        chain_id = new_id("qob")
        try:
            instrument = await self._instrument(instrument_id)
            reference = InstrumentRef(
                instrument.trading_symbol, instrument.exchange, instrument.segment
            )
            quote = await self.provider.get_quote(reference)
            as_of = self.validate_quote(quote, reference)
            await AuditService(self.clock).append(
                AuditIdentity(
                    chain_id=chain_id,
                    event_type="REFERENCE_QUOTE_SNAPSHOT",
                    actor="reference_ingestion",
                    mode=TradingMode.PAPER,
                ),
                {
                    "instrument_id": instrument_id,
                    "data_used": {
                        "as_of": as_of,
                        "data_origin": self.provider.data_origin,
                        "provider": self.provider.name,
                        "quote": {
                            key: value for key, value in asdict(quote).items() if key != "raw"
                        },
                    },
                },
            )
            return QuoteObservation(chain_id, quote)
        except Exception:
            await AuditService(self.clock).append(
                AuditIdentity(
                    chain_id=chain_id,
                    event_type="MARKET_INPUT_REJECTED",
                    actor="reference_ingestion",
                    mode=TradingMode.PAPER,
                ),
                {"instrument_id": instrument_id, "result": {"reason": "QUOTE_REFRESH_UNAVAILABLE"}},
            )
            raise

    async def _instrument(self, instrument_id):
        async with db_session.session_scope() as session:
            instrument = await session.scalar(
                sa.select(Instrument).where(
                    Instrument.id == instrument_id,
                    Instrument.is_active.is_(True),
                    Instrument.is_restricted.is_(False),
                )
            )
        if instrument is None:
            raise ValueError("instrument unavailable or restricted")
        return instrument

    def validate_quote(self, quote, reference):
        as_of = self.clock.now()
        if (
            quote.instrument != reference
            or quote.data_origin != self.provider.data_origin
            or quote.observed_at.utcoffset() is None
            or not timedelta(0) <= as_of - quote.observed_at <= self.max_quote_age
            or not quote.ltp.is_finite()
            or not validate_quote(quote).ok
            or not quote.bids
            or not quote.asks
            or any(
                not level.price.is_finite() or level.price <= 0 or level.quantity <= 0
                for level in (*quote.bids, *quote.asks)
            )
        ):
            raise ValueError("stale, future, invalid or unprovenanced quote")
        return as_of

    async def _store_observation(self, instrument_id, chain_id, quote, bars, as_of):
        closed_at = bars[-1].ts + timedelta(minutes=1)
        available = {
            "candles": ContextValue(
                observed_at=closed_at,
                available_at=as_of,
                origin=self.provider.data_origin,
                value=bars,
            )
        }
        validate_context_inputs(
            ("candles",),
            available,
            as_of=as_of,
            max_age=timedelta(seconds=60),
            origin=self.provider.data_origin,
            timeframe_seconds=60,
        )
        for index, bar in enumerate(bars):
            if not validate_bar(bar).ok or (
                index and bar.ts - bars[index - 1].ts != timedelta(minutes=1)
            ):
                raise ValueError("invalid or discontinuous candle window")
        indicators = {"sma20": sma([bar.close for bar in bars], 20)[-1], "atr14": atr(bars, 14)[-1]}
        available["indicators"] = ContextValue(
            observed_at=closed_at,
            available_at=as_of,
            origin=self.provider.data_origin,
            value=indicators,
        )
        async with db_session.session_scope() as session:
            result = await CandleStore(self.clock).write(
                instrument_id, 1, bars, data_origin=self.provider.data_origin, session=session
            )
            if result.rejected:
                raise ValueError("candle persistence rejected input")
            await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=chain_id,
                    event_type="REFERENCE_MARKET_SNAPSHOT",
                    actor="reference_ingestion",
                    mode=TradingMode.PAPER,
                ),
                {
                    "instrument_id": instrument_id,
                    "data_used": {
                        "as_of": as_of,
                        "data_origin": self.provider.data_origin,
                        "provider": self.provider.name,
                        "recording_evidence": (
                            self.provider.recording_evidence()
                            if callable(getattr(self.provider, "recording_evidence", None))
                            else None
                        ),
                        "candles": [asdict(bar) for bar in bars],
                        "quote": {
                            key: value for key, value in asdict(quote).items() if key != "raw"
                        },
                        "indicators": indicators,
                    },
                },
            )
        return ReferenceObservation(chain_id, available, quote)
