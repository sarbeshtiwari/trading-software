"""Dashboard amounts match known account budgets and canonical risk rules."""

from decimal import Decimal

from app.risk import rules
from app.risk.state import account_metrics
from tests.unit.test_risk import limits, market, portfolio, proposal


def test_absolute_limits_reserved_risk_and_rule_parity():
    account = portfolio(
        equity=90000, realised_day_pnl=-1000, unrealised_day_pnl=-200, reserved_risk=Decimal(300)
    )
    config = limits(
        max_daily_loss_amount=1500, max_drawdown_amount=8000, max_gross_exposure_amount=200000
    )
    metrics = {metric.name: metric for metric in account_metrics(account, config)}
    assert metrics["daily_loss"].used == 1200
    assert metrics["daily_loss"].limit == 1500
    assert metrics["daily_loss"].percent == 80
    assert metrics["daily_loss_with_reserved_risk"].used == 1500
    assert metrics["daily_loss_with_reserved_risk"].percent == 100
    assert metrics["drawdown"].used == 10000
    assert metrics["drawdown"].limit == 8000
    assert metrics["drawdown"].percent == 125
    assert metrics["gross_exposure"].limit == 200000
    for name in ("daily_loss", "drawdown", "exposure"):
        check = getattr(rules, name)(proposal(), account, market(), config)[0]
        metric = metrics["gross_exposure" if name == "exposure" else name]
        assert metric.limit == Decimal(check.inputs["limit"])


def test_zero_equity_never_invents_percentage_or_budget():
    metrics = {metric.name: metric for metric in account_metrics(portfolio(equity=0), limits())}
    assert metrics["daily_loss"].limit == 0
    assert metrics["daily_loss"].percent is None
    assert metrics["gross_exposure"].percent is None
