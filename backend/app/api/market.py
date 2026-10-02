"""Stored market observations; no live price synthesis or execution permission."""

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.analysis.technical.ma import sma
from app.core.clock import ensure_ist, get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.marketdata.ingest import CandleStore
from app.marketdata.models import Bar
from app.marketdata.staleness import bar_is_stale
from app.marketdata.validation import validate_bar

router = APIRouter(prefix="/market", tags=["market"])


class MarketInstrument(EvidenceModel):
    id: str
    symbol: str
    exchange: str
    segment: str
    active: bool
    restricted: bool


class InstrumentPage(EvidenceModel):
    instruments: tuple[MarketInstrument, ...]
    has_more: bool


class ChartBar(EvidenceModel):
    ts: AwareDatetime
    closed_at: AwareDatetime
    ingested_at: AwareDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    sma20: Decimal | None


class CandleChart(EvidenceModel):
    instrument_id: str
    origin: DataOrigin
    interval_minutes: int
    as_of: AwareDatetime
    status: str
    bars: tuple[ChartBar, ...] = ()
    discontinuities: int = 0
    scope: str = (
        "Stored observations only; not live LTP or tradability. No missing bars are inferred."
    )


@router.get("/instruments", response_model=InstrumentPage)
async def instruments(
    query: str = Query(default="", max_length=80), offset: int = Query(default=0, ge=0)
):
    statement = sa.select(Instrument)
    if query.strip():
        statement = statement.where(
            Instrument.trading_symbol.contains(query.strip().upper(), autoescape=True)
        )
    async with db_session.session_scope() as session:
        rows = list(
            await session.scalars(
                statement.order_by(Instrument.trading_symbol, Instrument.id)
                .offset(offset)
                .limit(51)
            )
        )
    return InstrumentPage(
        instruments=tuple(
            MarketInstrument(
                id=row.id,
                symbol=row.trading_symbol,
                exchange=row.exchange.value,
                segment=row.segment.value,
                active=row.is_active,
                restricted=row.is_restricted,
            )
            for row in rows[:50]
        ),
        has_more=len(rows) > 50,
    )


@router.get("/candles/{instrument_id}", response_model=CandleChart)
async def candles(
    instrument_id: str,
    origin: DataOrigin,
    interval: int = Query(default=1),
    as_of: AwareDatetime | None = None,
    limit: int = Query(default=300, ge=1, le=1000),
):
    if interval not in (1, 5, 10, 60, 240):
        raise HTTPException(422, "Unsupported chart interval")
    cutoff = as_of or get_clock().now()
    if cutoff > get_clock().now():
        raise HTTPException(422, "Future chart cutoff refused")
    async with db_session.session_scope() as session:
        if await session.get(Instrument, instrument_id) is None:
            raise HTTPException(404, "Instrument unavailable")
    result = CandleChart(
        instrument_id=instrument_id,
        origin=origin,
        interval_minutes=interval,
        as_of=cutoff,
        status="UNAVAILABLE",
    )
    rows = await CandleStore().closed_observations(
        instrument_id, interval, origin=origin, as_of=cutoff, limit=limit
    )
    if not rows:
        return result
    observations = [
        Bar(
            ts=ensure_ist(row.ts),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            open_interest=row.open_interest,
        )
        for row in rows
    ]
    if any(
        not all(price.is_finite() for price in (bar.open, bar.high, bar.low, bar.close))
        or not validate_bar(bar).ok
        for bar in observations
    ):
        return result.model_copy(update={"status": "INVALID_STORED_DATA"})
    averages = (
        sma([bar.close for bar in observations], 20)
        if len(observations) >= 20
        else [None] * len(observations)
    )
    span = timedelta(minutes=interval)
    bars = tuple(
        ChartBar(
            ts=bar.ts,
            closed_at=bar.ts + span,
            ingested_at=_utc(row.ingested_at),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            sma20=average,
        )
        for row, bar, average in zip(rows, observations, averages, strict=True)
    )
    gaps = sum(
        current.ts - previous.ts != span
        for previous, current in pairwise(observations)
    )
    return result.model_copy(
        update={
            "bars": bars,
            "discontinuities": gaps,
            "status": "STALE" if bar_is_stale(bars[-1].ts, interval) else "RECORDED",
        }
    )
