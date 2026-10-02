"""Independent hand-computed net-series references and undefined boundaries."""

from decimal import Decimal

import pytest

from app.portfolio.metrics import closed_trade_drawdown_amount, performance


def decimals(values):
    return [Decimal(value) if value is not None else None for value in values]


def test_closed_trade_drawdown_uses_zero_initial_pnl_not_invented_capital():
    assert closed_trade_drawdown_amount(decimals([100, -40, -80, 30])) == 120
    assert closed_trade_drawdown_amount(decimals([-5, -10, 20, -3])) == 15
    assert closed_trade_drawdown_amount(decimals([10, 20])) == 0
    assert closed_trade_drawdown_amount([]) is None
    assert closed_trade_drawdown_amount(decimals([10, None, -5])) is None
    with pytest.raises(ValueError):
        closed_trade_drawdown_amount([Decimal("NaN")])


def test_drawdown_net_trade_statistics_and_recovery_reference():
    result = performance(
        decimals([100, 120, 90, 110]), decimals([20, -30, 20]), decimals([60, 120, 180])
    )
    assert result["total_return"] == Decimal(".1")
    assert result["max_drawdown"] == Decimal(".25")
    assert result["drawdown"][:3] == decimals([0, 0, ".25"])
    assert result["drawdown"][3] == Decimal(1) / 12
    assert result["recovery_factor"] == Decimal(1) / 3
    assert result["win_rate"] == Decimal(2) / 3
    assert result["average_win"] == 20
    assert result["average_loss"] == -30
    assert result["profit_factor"] == Decimal(4) / 3
    assert result["expectancy"] == Decimal(10) / 3
    assert result["average_holding_seconds"] == 120


def test_no_interpolation_across_unknown_equity_or_net_trade_costs():
    result = performance(decimals([100, None, 120]), decimals([None]), decimals([60]))
    assert result["total_return"] is None and result["max_drawdown"] is None
    assert result["drawdown"] == [None, None, None]
    assert result["expectancy"] is None and result["win_rate"] is None
    assert result["trade_count"] == 1


def test_no_trades_all_wins_zero_losses_and_insolvency():
    empty = performance(decimals([100, 100]), [], [])
    assert empty["total_return"] == 0 and empty["max_drawdown"] == 0
    assert empty["expectancy"] is None and empty["profit_factor"] is None
    result = performance(decimals([100, 120]), decimals([20, 0]), decimals([1, 1]))
    assert result["win_rate"] == Decimal(".5")
    assert result["profit_factor"] is None and result["recovery_factor"] is None
    ruined = performance(decimals([100, -20]), decimals([-120]), decimals([60]))
    assert ruined["max_drawdown"] == Decimal("1.2")
    assert ruined["total_return"] == Decimal("-1.2")
    assert ruined["profit_factor"] == 0


@pytest.mark.parametrize(
    "equity,pnl,holding",
    [
        ([0, 10], [], []),
        ([100, "NaN"], [], []),
        ([100, 90], ["Infinity"], [1]),
        ([100, 90], [-10], [-1]),
        ([100, 90], [-10], []),
    ],
)
def test_malformed_observations_are_rejected(equity, pnl, holding):
    with pytest.raises(ValueError):
        performance(decimals(equity), decimals(pnl), decimals(holding))
