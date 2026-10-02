"""Read-only point-in-time fundamental evidence and transparent analyses."""

from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.analysis.fundamental.health import BalanceHealth, balance_health
from app.analysis.fundamental.quality import growth_profitability
from app.analysis.fundamental.score import FundamentalScore, fundamental_score
from app.analysis.fundamental.source import FundamentalView
from app.analysis.fundamental.store import FundamentalStore
from app.analysis.fundamental.valuation import valuation_metrics
from app.core.clock import get_clock

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
