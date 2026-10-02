"""Read-only point-in-time fundamental evidence and transparent analyses."""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime

from app.analysis.fundamental.health import balance_health
from app.analysis.fundamental.quality import growth_profitability
from app.analysis.fundamental.score import fundamental_score
from app.analysis.fundamental.store import FundamentalStore
from app.analysis.fundamental.valuation import valuation_metrics
from app.core.clock import get_clock

router = APIRouter(prefix="/fundamentals", tags=["fundamentals"])


@router.get("/{instrument_id}")
async def fundamentals(
    instrument_id: str,
    source: str,
    as_of: AwareDatetime | None = None,
    max_age_days: int = Query(365, ge=1, le=3650),
) -> dict:
    cutoff = as_of or get_clock().now()
    if cutoff > get_clock().now():
        raise HTTPException(status_code=422, detail="future as_of is not permitted")
    view = await FundamentalStore().at(
        instrument_id, source=source, as_of=cutoff, max_age=timedelta(days=max_age_days)
    )
    valuation = valuation_metrics(view)
    quality = growth_profitability(view)
    return {
        "status": "AVAILABLE" if view.record_id is not None else "UNAVAILABLE",
        "view": view.model_dump(mode="json"),
        "valuation": {
            name: str(value) if value is not None else None for name, value in valuation.items()
        },
        "quality": {
            name: str(value) if value is not None else None for name, value in quality.items()
        },
        "health": balance_health(view).model_dump(mode="json"),
        "score": fundamental_score(view).model_dump(mode="json"),
    }
