"""FUND-004: inspectable balance-sheet components; missing evidence never scores zero."""

from decimal import Decimal

from app.analysis.equity import EvidenceModel
from app.analysis.fundamental.quality import ratio
from app.analysis.fundamental.source import FundamentalView


class BalanceHealth(EvidenceModel):
    metrics: dict[str, Decimal | None]
    components: dict[str, Decimal | None]
    score: Decimal | None
    formula: str = "BALANCE_HEALTH_V1"


def balance_health(view: FundamentalView) -> BalanceHealth:
    pledge = view.metrics.get("promoter_pledge")
    metrics = {
        "debt_to_equity": ratio(view, "debt", "equity"),
        "interest_coverage": ratio(view, "ebit", "interest_expense"),
        "current_ratio": ratio(view, "current_assets", "current_liabilities"),
        "promoter_pledge": pledge.value if pledge else None,
    }
    thresholds = {
        "debt_to_equity": Decimal(1),
        "interest_coverage": Decimal(3),
        "current_ratio": Decimal(1),
        "promoter_pledge": Decimal(0),
    }
    components = {}
    for name, value in metrics.items():
        passing = value is not None and (
            value <= thresholds[name]
            if name in ("debt_to_equity", "promoter_pledge")
            else value >= thresholds[name]
        )
        components[name] = Decimal(int(passing)) if value is not None else None
    score = (
        sum(components.values()) / 4
        if all(value is not None for value in components.values())
        else None
    )
    return BalanceHealth(metrics=metrics, components=components, score=score)
