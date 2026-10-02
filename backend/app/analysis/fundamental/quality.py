"""FUND-003: profitability and growth fractions; non-positive bases are unavailable."""

from decimal import Decimal

from app.analysis.fundamental.source import FundamentalView


def ratio(view: FundamentalView, numerator: str, denominator: str) -> Decimal | None:
    top, bottom = view.metrics.get(numerator), view.metrics.get(denominator)
    if top is None or bottom is None or bottom.value <= 0:
        return None
    return top.value / bottom.value


def growth_profitability(view: FundamentalView) -> dict[str, Decimal | None]:
    revenue = ratio(view, "revenue", "previous_revenue")
    eps = ratio(view, "eps", "previous_eps")
    return {
        "revenue_growth": revenue - 1 if revenue is not None else None,
        "eps_growth": eps - 1 if eps is not None else None,
        "roe": ratio(view, "net_income", "equity"),
        "roce": ratio(view, "ebit", "capital_employed"),
        "operating_margin": ratio(view, "operating_profit", "revenue"),
        "net_margin": ratio(view, "net_income", "revenue"),
    }
