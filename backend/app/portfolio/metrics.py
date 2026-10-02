"""Shared descriptive net-performance metrics; never an approval or forecast."""

from decimal import Decimal


def _validate(values):
    if any(
        value is not None and (not isinstance(value, Decimal) or not value.is_finite())
        for value in values
    ):
        raise ValueError("finite Decimal observations or explicit unavailable values required")


def _checked_inputs(equity, trade_pnl, holding_seconds):
    equity, trade_pnl, holding_seconds = tuple(equity), tuple(trade_pnl), tuple(holding_seconds)
    _validate((*equity, *trade_pnl, *holding_seconds))
    if len(holding_seconds) != len(trade_pnl) or any(
        value is not None and value < 0 for value in holding_seconds
    ):
        raise ValueError("one nonnegative holding interval per trade required")
    if equity and equity[0] is not None and equity[0] <= 0:
        raise ValueError("positive initial equity required")
    return equity, trade_pnl, holding_seconds


def performance(equity, trade_pnl, holding_seconds):
    equity, trade_pnl, holding_seconds = _checked_inputs(equity, trade_pnl, holding_seconds)
    result = {
        "trade_count": len(trade_pnl),
        "total_return": None,
        "max_drawdown": None,
        "recovery_factor": None,
        "win_rate": None,
        "average_win": None,
        "average_loss": None,
        "profit_factor": None,
        "expectancy": None,
        "average_holding_seconds": None,
        "drawdown": [None] * len(equity),
        "unavailable": {},
    }
    if len(equity) >= 2 and all(value is not None for value in equity):
        peak = equity[0]
        drawdown = []
        amount = Decimal(0)
        for value in equity:
            peak = max(peak, value)
            amount = max(amount, peak - value)
            drawdown.append((peak - value) / peak)
        result.update(
            total_return=equity[-1] / equity[0] - 1, max_drawdown=max(drawdown), drawdown=drawdown
        )
        if amount:
            result["recovery_factor"] = (equity[-1] - equity[0]) / amount
        else:
            result["unavailable"]["recovery_factor"] = "NO_DRAWDOWN_DENOMINATOR"
    else:
        result["unavailable"]["equity_metrics"] = "INSUFFICIENT_OR_UNAVAILABLE_EQUITY"
    if trade_pnl and all(value is not None for value in trade_pnl):
        wins = [value for value in trade_pnl if value > 0]
        losses = [value for value in trade_pnl if value < 0]
        result["win_rate"] = Decimal(len(wins)) / len(trade_pnl)
        result["expectancy"] = sum(trade_pnl, Decimal(0)) / len(trade_pnl)
        result["average_win"] = sum(wins, Decimal(0)) / len(wins) if wins else None
        result["average_loss"] = sum(losses, Decimal(0)) / len(losses) if losses else None
        if losses:
            result["profit_factor"] = sum(wins, Decimal(0)) / -sum(losses, Decimal(0))
        else:
            result["unavailable"]["profit_factor"] = "NO_LOSS_DENOMINATOR"
    else:
        result["unavailable"]["trade_metrics"] = "NO_TRADES_OR_UNAVAILABLE_NET_PNL"
    if holding_seconds and all(value is not None for value in holding_seconds):
        result["average_holding_seconds"] = sum(holding_seconds, Decimal(0)) / len(holding_seconds)
    else:
        result["unavailable"]["average_holding_seconds"] = "HOLDING_INTERVALS_UNAVAILABLE"
    return result


def closed_trade_drawdown_amount(trade_pnl):
    values = tuple(trade_pnl)
    _validate(values)
    if not values or any(value is None for value in values):
        return None
    cumulative = peak = maximum = Decimal(0)
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum
