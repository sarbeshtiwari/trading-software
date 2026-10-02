"""Explicit provider-backed regime observations, retaining source timestamps."""

from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.analysis.regime.inputs import Observation
from app.core.enums import InstrumentType, Segment
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.fno.chain.model import require_as_of
from app.fno.chain.skew import term_structure
from app.marketdata.models import InstrumentRef


class ProviderRegimeSource(EvidenceModel):
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    breadth_instrument_ids: tuple[str, ...] = Field(min_length=1, max_length=500)
    option_underlying: str = Field(min_length=1)
    option_expiry: date

    @model_validator(mode="after")
    def coherent(self):
        if not self.known_at <= self.valid_from < self.valid_until:
            raise ValueError("invalid provider regime policy window")
        if self.valid_until - self.valid_from > timedelta(days=1):
            raise ValueError("provider regime policy cannot exceed one day")
        if len(set(self.breadth_instrument_ids)) != len(self.breadth_instrument_ids):
            raise ValueError("duplicate breadth constituent")
        if any(not identifier.strip() for identifier in self.breadth_instrument_ids):
            raise ValueError("empty breadth constituent")
        return self


async def _references(source):
    references = []
    async with db_session.session_scope() as session:
        for identifier in source.breadth_instrument_ids:
            instrument = await session.get(Instrument, identifier)
            if (
                instrument is None
                or not instrument.is_active
                or instrument.instrument_type != InstrumentType.EQUITY
                or instrument.segment != Segment.CASH
            ):
                raise ValueError("breadth constituent unavailable")
            references.append(
                InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
            )
    if len({reference.key for reference in references}) != len(references):
        raise ValueError("duplicate breadth market identity")
    return references


def _breadth(quotes, references, *, origin, as_of, max_age, identifier):
    if set(quotes) != {reference.key for reference in references}:
        raise ValueError("incomplete or unexpected breadth response")
    observations = []
    advances = 0
    for reference in references:
        quote = quotes[reference.key]
        if (
            quote.instrument != reference
            or quote.data_origin != origin
            or quote.observed_at.utcoffset() is None
            or not timedelta(0) <= as_of - quote.observed_at <= max_age
            or quote.previous_close is None
            or any(
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                for value in (quote.open, quote.high, quote.low, quote.close, quote.previous_close)
            )
            or not quote.low
            <= min(quote.open, quote.close)
            <= max(quote.open, quote.close)
            <= quote.high
        ):
            raise ValueError("invalid or stale breadth observation")
        advances += int(quote.close > quote.previous_close)
        observations.append(quote.observed_at)
    return Observation(
        value=Decimal(advances) / len(references),
        observed_at=min(observations),
        available_at=as_of,
        source=identifier,
    )


async def collect_regime_observations(provider, source, *, clock, max_age, identifier):
    now = clock.now()
    if not source.known_at <= source.valid_from <= now < source.valid_until:
        raise ValueError("provider regime policy unavailable")
    references = await _references(source)
    quotes = await provider.get_ohlc(references)
    chain = await provider.get_option_chain(source.option_underlying, source.option_expiry)
    as_of = clock.now()
    if as_of >= source.valid_until:
        raise ValueError("provider regime policy expired during collection")
    breadth = _breadth(
        quotes,
        references,
        origin=provider.data_origin,
        as_of=as_of,
        max_age=max_age,
        identifier=identifier,
    )
    if (
        chain.underlying != source.option_underlying
        or chain.expiry != source.option_expiry
        or chain.data_origin != provider.data_origin
    ):
        raise ValueError("option-chain regime source mismatch")
    require_as_of(chain, as_of, max_age)
    point = term_structure([chain], as_of=as_of, max_age=max_age)[0].atm
    volatility = None
    if point is not None and point.call_iv is not None and point.put_iv is not None:
        row = next(row for row in chain.strikes if row.strike == point.strike)
        timestamps = [chain.observed_at]
        for leg in (row.call, row.put):
            if leg.greeks.computed_at is None:
                raise ValueError("IV source timestamp unavailable")
            timestamps.append(leg.greeks.computed_at)
        observed_at = min(timestamps)
        if as_of - observed_at > max_age:
            raise ValueError("stale implied volatility")
        volatility = Observation(
            value=(point.call_iv + point.put_iv) / 2,
            observed_at=observed_at,
            available_at=as_of,
            source=identifier,
        )
    evidence = {
        "policy": source.model_dump(mode="json"),
        "breadth_method": "ADVANCES_OVER_ALL_DECLARED_CONSTITUENTS_UNCHANGED_IN_DENOMINATOR",
        "iv_method": "MEAN_ATM_CALL_PUT_PERCENT_LOWER_STRIKE_ON_TIE",
        "ohlc": [asdict(quotes[reference.key]) for reference in references],
        "option_chain": {key: value for key, value in asdict(chain).items() if key != "raw"},
    }
    return volatility, breadth, evidence
