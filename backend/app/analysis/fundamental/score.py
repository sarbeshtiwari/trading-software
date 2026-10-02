"""FUND-005: versioned quality/health score with inspectable contributions."""

from decimal import Decimal

from app.analysis.equity import EvidenceModel
from app.analysis.fundamental.health import balance_health
from app.analysis.fundamental.quality import growth_profitability
from app.analysis.fundamental.source import FundamentalView


class FundamentalScore(EvidenceModel):
    formula: str = "QUALITY_HEALTH_V1"
    record_id: str | None
    components: dict[str, Decimal | None]
    score: Decimal | None


def fundamental_score(view: FundamentalView) -> FundamentalScore:
    quality = growth_profitability(view)
    components = {
        "balance_health": balance_health(view).score,
        "positive_revenue_growth": None
        if quality["revenue_growth"] is None
        else Decimal(int(quality["revenue_growth"] > 0)),
        "positive_roe": None if quality["roe"] is None else Decimal(int(quality["roe"] > 0)),
    }
    score = (
        sum(components.values()) / 3
        if all(value is not None for value in components.values())
        else None
    )
    return FundamentalScore(record_id=view.record_id, components=components, score=score)
