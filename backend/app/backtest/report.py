"""Translate persisted account/journal evidence into shared descriptive metrics."""

from decimal import Decimal

from app.agents.validation import _utc
from app.portfolio.metrics import performance


def summarise(samples, trades):
    values = [
        Decimal(sample["net_equity"]) if sample["net_equity"] is not None else None
        for sample in samples
    ]
    holding = [
        Decimal(str((_utc(trade.closed_at) - _utc(trade.opened_at)).total_seconds()))
        if trade.opened_at is not None and trade.closed_at is not None
        else None
        for trade in trades
    ]
    result = performance(values, [trade.net_pnl for trade in trades], holding)
    result["unavailable"]["annualized_metrics"] = "NO_VERIFIED_REGULAR_RETURN_SERIES_OR_BENCHMARK"
    return result


def json_metrics(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_metrics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_metrics(item) for item in value]
    return value
