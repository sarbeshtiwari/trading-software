"""Provider-fed index analysis with explicit, non-renewable external evidence."""

from dataclasses import asdict
from datetime import timedelta

from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.analysis.regime.classifier import RegimePolicy
from app.analysis.regime.events import EventCalendar
from app.analysis.regime.history import RegimeStore
from app.analysis.regime.inputs import IndicatorPolicy, Observation, build_inputs
from app.audit.service import AuditIdentity, AuditService
from app.core.enums import InstrumentType
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.marketdata.ingest import CandleStore
from app.marketdata.models import InstrumentRef
from app.marketdata.validation import validate_bar
from app.modes import TradingMode
from app.strategies.context import ContextValue, validate_context_inputs
from app.trading.regime_sources import ProviderRegimeSource, collect_regime_observations


class RegimeSource(EvidenceModel):
    index_instrument_id: str = Field(min_length=1)
    indicators: IndicatorPolicy
    policy: RegimePolicy
    implied_volatility: Observation | None
    breadth: Observation | None
    calendar: EventCalendar | None
    provider_source: ProviderRegimeSource | None = None

    @property
    def lookback(self):
        return max(
            self.indicators.adx_period * 2 + 1,
            self.indicators.slow_period,
            self.indicators.volatility_period + 1,
        )

    @model_validator(mode="after")
    def bounded(self):
        if self.provider_source is not None and (
            self.implied_volatility is not None or self.breadth is not None
        ):
            raise ValueError("manual and provider regime observations cannot be mixed")
        if self.indicators.bar_seconds != 60 or self.lookback > 375:
            raise ValueError(
                "reference regime requires one-minute bars and at most 375 observations"
            )
        if self.implied_volatility is not None and not 0 <= self.implied_volatility.value <= 10000:
            raise ValueError("invalid implied volatility")
        if self.breadth is not None and not 0 <= self.breadth.value <= 1:
            raise ValueError("invalid breadth")
        return self

    def require_known(self, as_of):
        if self.provider_source is not None and self.provider_source.known_at > as_of:
            raise ValueError("future provider regime policy")
        if any(
            item is not None and item.available_at > as_of
            for item in (self.implied_volatility, self.breadth)
        ):
            raise ValueError("future regime source evidence")
        if self.calendar is not None and self.calendar.known_at > as_of:
            raise ValueError("future event calendar evidence")


async def refresh_regime(provider, source, *, underlying, clock):
    source.require_known(clock.now())
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, source.index_instrument_id)
    if (
        instrument is None
        or not instrument.is_active
        or instrument.instrument_type != InstrumentType.INDEX
    ):
        raise ValueError("regime index instrument unavailable")
    reference = InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
    end = clock.now().replace(second=0, microsecond=0)
    bars = tuple(
        await provider.get_candles(
            reference, 1, end - timedelta(minutes=source.lookback), end - timedelta(minutes=1)
        )
    )
    as_of = clock.now()
    if len(bars) != source.lookback or any(
        not validate_bar(bar).ok or (index and bar.ts - bars[index - 1].ts != timedelta(minutes=1))
        for index, bar in enumerate(bars)
    ):
        raise ValueError("regime requires complete valid closed index candles")
    closed = bars[-1].ts + timedelta(minutes=1)
    validate_context_inputs(
        ("candles",),
        {
            "candles": ContextValue(
                observed_at=closed, available_at=as_of, origin=provider.data_origin, value=bars
            )
        },
        as_of=as_of,
        max_age=timedelta(seconds=60),
        origin=provider.data_origin,
        timeframe_seconds=60,
    )
    identifier = new_id("rgs")
    volatility, breadth, provider_evidence = source.implied_volatility, source.breadth, None
    if source.provider_source is not None:
        volatility, breadth, provider_evidence = await collect_regime_observations(
            provider,
            source.provider_source,
            clock=clock,
            max_age=timedelta(seconds=source.policy.max_age_seconds),
            identifier=identifier,
        )
        as_of = clock.now()
    inputs = build_inputs(
        bars,
        underlying=underlying,
        as_of=as_of,
        bars_available_at=as_of,
        source=identifier,
        data_origin=provider.data_origin,
        policy=source.indicators,
        implied_volatility=volatility,
        breadth=breadth,
        calendar=source.calendar,
    )
    if source.provider_source is not None:
        inputs = inputs.model_copy(update={"valid_until": source.provider_source.valid_until})
    async with db_session.session_scope() as session:
        result = await CandleStore(clock).write(
            instrument.id, 1, bars, data_origin=provider.data_origin, session=session
        )
        if result.rejected:
            raise ValueError("regime candle persistence rejected input")
        await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier,
                event_type="REFERENCE_REGIME_INPUTS",
                actor="reference_regime",
                mode=TradingMode.PAPER,
            ),
            {
                "instrument_id": instrument.id,
                "data_used": {
                    "provider": provider.name,
                    "recording_evidence": (
                        provider.recording_evidence()
                        if callable(getattr(provider, "recording_evidence", None))
                        else None
                    ),
                    "data_origin": provider.data_origin,
                    "underlying": underlying,
                    "as_of": as_of,
                    "candles": [asdict(bar) for bar in bars],
                    "source": source.model_dump(mode="json"),
                    "provider_observations": provider_evidence,
                },
            },
            expected_count=0,
        )
    return await RegimeStore(clock).record(inputs, source.policy)
