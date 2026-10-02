"""Read-only point-in-time fundamental evidence and transparent analyses."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.analysis.fundamental.health import BalanceHealth, balance_health
from app.analysis.fundamental.quality import growth_profitability
from app.analysis.fundamental.score import FundamentalScore, fundamental_score
from app.analysis.fundamental.source import FundamentalView
from app.analysis.fundamental.store import FundamentalStore
from app.analysis.fundamental.valuation import valuation_metrics
from app.core.clock import UTC, get_clock
from app.db import session as db_session
from app.db.models.fundamental_versions import FundamentalVersion

router = APIRouter(prefix="/fundamentals", tags=["fundamentals"])


class FundamentalDetail(EvidenceModel):
    instrument_id: str
    source: str
    as_of: AwareDatetime
    max_age_days: int
    status: str
    view: FundamentalView
    valuation: dict[str, Decimal | None]
    quality: dict[str, Decimal | None]
    health: BalanceHealth
    score: FundamentalScore


class FundamentalSourceReceipt(EvidenceModel):
    source: str
    record_id: str
    known_at: AwareDatetime
    received_at: AwareDatetime


class FundamentalSources(EvidenceModel):
    instrument_id: str
    as_of: AwareDatetime
    sources: tuple[FundamentalSourceReceipt, ...]
    has_more: bool


@router.get("/{instrument_id}/sources", response_model=FundamentalSources)
async def sources(
    instrument_id: str,
    as_of: AwareDatetime | None = None,
    offset: int = Query(default=0, ge=0),
) -> FundamentalSources:
    cutoff = as_of or get_clock().now()
    if cutoff > get_clock().now():
        raise HTTPException(422, "future as_of is not permitted")
    ranked = (
        sa.select(
            FundamentalVersion.id,
            FundamentalVersion.source,
            FundamentalVersion.known_at,
            FundamentalVersion.received_at,
            sa.func.row_number()
            .over(
                partition_by=FundamentalVersion.source,
                order_by=FundamentalVersion.known_at.desc(),
            )
            .label("rank"),
        )
        .where(
            FundamentalVersion.instrument_id == instrument_id,
            FundamentalVersion.known_at <= cutoff.astimezone(UTC),
            FundamentalVersion.received_at <= cutoff.astimezone(UTC),
        )
        .subquery()
    )
    statement = sa.select(ranked).where(ranked.c.rank == 1).order_by(ranked.c.source)
    async with db_session.session_scope() as session:
        rows = (await session.execute(statement.offset(offset).limit(51))).mappings().all()
    return FundamentalSources(
        instrument_id=instrument_id,
        as_of=cutoff,
        sources=tuple(
            FundamentalSourceReceipt(
                source=row["source"],
                record_id=row["id"],
                known_at=row["known_at"].replace(tzinfo=UTC)
                if row["known_at"].tzinfo is None
                else row["known_at"],
                received_at=row["received_at"].replace(tzinfo=UTC)
                if row["received_at"].tzinfo is None
                else row["received_at"],
            )
            for row in rows[:50]
        ),
        has_more=len(rows) > 50,
    )


@router.get("/{instrument_id}", response_model=FundamentalDetail)
async def fundamentals(
    instrument_id: str,
    source: str,
    as_of: AwareDatetime | None = None,
    max_age_days: int = Query(365, ge=1, le=3650),
) -> FundamentalDetail:
    cutoff = as_of or get_clock().now()
    if cutoff > get_clock().now():
        raise HTTPException(status_code=422, detail="future as_of is not permitted")
    view = await FundamentalStore().at(
        instrument_id, source=source, as_of=cutoff, max_age=timedelta(days=max_age_days)
    )
    valuation = valuation_metrics(view)
    quality = growth_profitability(view)
    return FundamentalDetail(
        instrument_id=instrument_id,
        source=source,
        as_of=cutoff,
        max_age_days=max_age_days,
        status="AVAILABLE" if view.record_id is not None else "UNAVAILABLE",
        view=view,
        valuation=valuation,
        quality=quality,
        health=balance_health(view),
        score=fundamental_score(view),
    )
