"""FUND-002: named valuation metrics, retaining missing/freshness exclusions."""

from decimal import Decimal

from app.analysis.fundamental.source import FundamentalView


def valuation_metrics(view: FundamentalView) -> dict[str, Decimal | None]:
    return {
        name: view.metrics[name].value if view.metrics.get(name) is not None else None
        for name in ("pe_ratio", "pb_ratio", "ev_ebitda", "dividend_yield", "market_cap")
    }
