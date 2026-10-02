"""Provider-backed option evidence for the existing PAPER reference worker."""

from dataclasses import asdict, replace
from datetime import timedelta

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.core.enums import Exchange, GreekSource, InstrumentType, OptionType, Segment
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.fno.chain.model import chain_adapter, require_as_of
from app.fno.chain.snapshots import ChainSnapshotStore
from app.modes import TradingMode
from app.risk.models import EvidenceTime


def contract_snapshot(instrument):
    return {
        "symbol": instrument.trading_symbol,
        "underlying": instrument.underlying,
        "expiry": instrument.expiry_date.isoformat(),
        "strike": str(instrument.strike_price),
        "option_type": instrument.option_type.value,
        "lot_size": instrument.lot_size,
        "tick_size": str(instrument.tick_size),
    }


async def require_option_observation(instrument, evidence, *, as_of, origin, max_age_seconds):
    if (
        evidence is None
        or as_of.utcoffset() is None
        or max_age_seconds <= 0
        or instrument.exchange != Exchange.NSE
        or instrument.segment != Segment.FNO
        or not instrument.is_active
        or not instrument.is_option
        or instrument.expiry_date is None
        or instrument.option_type is None
        or evidence.data_origin != origin
        or not evidence.observed_at <= evidence.available_at <= as_of
        or as_of - evidence.observed_at > timedelta(seconds=max_age_seconds)
    ):
        raise ValueError("option observation unavailable or stale")
    async with db_session.session_scope() as session:
        row = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.chain_id == evidence.source_id,
                AuditEvent.event_type == "PAPER_OPTION_OBSERVATION",
            )
        )
    if (
        row is None
        or row.instrument_id != instrument.id
        or row.mode != TradingMode.PAPER
        or row.actor != "reference_option_producer"
        or (
            row.occurred_at.replace(tzinfo=UTC)
            if row.occurred_at.tzinfo is None
            else row.occurred_at.astimezone(UTC)
        )
        > as_of
        or not await AuditService().verify(evidence.source_id, expected_count=1)
        or EvidenceTime.model_validate(row.result) != evidence
        or row.data_used.get("contract") != contract_snapshot(instrument)
    ):
        raise ValueError("option observation identity or audit mismatch")
    chain = chain_adapter.validate_python(row.data_used["chain"])
    require_as_of(chain, as_of, timedelta(seconds=max_age_seconds))
    if (chain.underlying, chain.expiry, chain.data_origin) != (
        instrument.underlying,
        instrument.expiry_date,
        origin,
    ):
        raise ValueError("option observation chain mismatch")
    return replace(chain, strikes=tuple(chain.strikes))


class OptionEvidenceSource:
    def __init__(self, provider, *, clock=None):
        self.provider = provider
        self.clock = clock or get_clock()

    async def observe(self, instrument, *, origin, max_age_seconds):
        if (
            instrument.segment != Segment.FNO
            or instrument.exchange != Exchange.NSE
            or instrument.instrument_type != InstrumentType.OPTION
            or not instrument.is_active
            or not instrument.underlying
            or instrument.expiry_date is None
            or instrument.strike_price is None
            or instrument.option_type not in (OptionType.CE, OptionType.PE)
            or self.provider.data_origin != origin
            or max_age_seconds <= 0
        ):
            raise ValueError("option contract evidence unavailable")
        chain = await self.provider.get_option_chain(instrument.underlying, instrument.expiry_date)
        now = self.clock.now()
        age = timedelta(seconds=max_age_seconds)
        require_as_of(chain, now, age)
        if (chain.underlying, chain.expiry, chain.data_origin) != (
            instrument.underlying,
            instrument.expiry_date,
            origin,
        ):
            raise ValueError("option chain identity or origin mismatch")
        matching = [row for row in chain.strikes if row.strike == instrument.strike_price]
        if len(matching) != 1:
            raise ValueError("exact option strike unavailable")
        leg = matching[0].call if instrument.option_type == OptionType.CE else matching[0].put
        if leg is None or leg.trading_symbol != instrument.trading_symbol:
            raise ValueError("exact option symbol unavailable")
        greeks = leg.greeks
        if (
            greeks is None
            or greeks.source != GreekSource.BROKER
            or greeks.computed_at is None
            or not timedelta(0) <= now - greeks.computed_at <= age
            or any(
                getattr(greeks, field) is None
                for field in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility")
            )
        ):
            raise ValueError("complete timestamped provider Greeks unavailable")
        if (
            not -1 <= greeks.delta <= 1
            or greeks.gamma < 0
            or greeks.vega < 0
            or greeks.implied_volatility <= 0
            or (instrument.option_type == OptionType.CE and greeks.delta < 0)
            or (instrument.option_type == OptionType.PE and greeks.delta > 0)
        ):
            raise ValueError("invalid provider Greek values")
        await ChainSnapshotStore(cadence=timedelta(microseconds=1), clock=self.clock).capture(chain)
        identifier = new_id("opt")
        evidence = EvidenceTime(
            source_id=identifier,
            observed_at=greeks.computed_at,
            available_at=self.clock.now(),
            data_origin=origin,
        )
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=identifier,
                event_type="PAPER_OPTION_OBSERVATION",
                actor="reference_option_producer",
                mode=TradingMode.PAPER,
            ),
            {
                "instrument_id": instrument.id,
                "data_used": {
                    "chain": chain_adapter.dump_python(chain, mode="json"),
                    "contract": contract_snapshot(instrument),
                    "greeks": asdict(greeks),
                },
                "result": evidence.model_dump(mode="json"),
            },
        )
        return evidence
