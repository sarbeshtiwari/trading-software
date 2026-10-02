"""Synthetic, hand-computed risk boundaries; no trade or performance claims."""

import builtins
import socket
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.data_origin import DataOrigin
from app.risk import engine, rules
from app.risk.config import RiskLimits
from app.risk.engine import evaluate
from app.risk.models import MarketState, PortfolioState, RiskProposal
from tests.unit.test_option_chain import OBSERVED


def limits(**changes):
    values = {
        "version": 1,
        "capital": 100000,
        "per_trade_risk_pct": "0.5",
        "daily_loss_limit_pct": 2,
        "max_drawdown_pct": 10,
        "max_gross_exposure_multiple": 3,
        "max_instrument_exposure_pct": 50,
        "max_sector_exposure_pct": 60,
        "max_underlying_exposure_pct": 50,
        "max_concurrent_positions": 5,
        "max_positions_per_strategy": 2,
        "min_stop_distance_pct": "0.1",
        "max_stop_distance_pct": 10,
        "min_reward_risk_ratio": "1.5",
        "margin_buffer_pct": 20,
        "max_slippage_risk_pct": 10,
        "max_market_age_seconds": 60,
        "max_portfolio_age_seconds": 30,
        "max_greeks_age_seconds": 60,
    }
    values.update(changes)
    return RiskLimits(**values)


def proposal(**changes):
    values = {
        "id": "prp-fixture",
        "instrument_id": "test-equity",
        "strategy_id": "test-strategy",
        "sector": "TEST_SECTOR",
        "underlying": "TEST",
        "generated_at": OBSERVED,
        "data_origin": DataOrigin.SYNTHETIC,
        "direction": "LONG",
        "quantity": 100,
        "lot_size": 1,
        "tick_size": "0.05",
        "entry": 100,
        "stop": 98,
        "first_target": 104,
        "exposure_per_unit": 100,
        "margin_per_unit": 100,
        "risk_cost_per_unit": 0,
        "is_option": False,
    }
    values.update(changes)
    return RiskProposal(**values)


def provenance():
    return {
        "observed_at": OBSERVED,
        "available_at": OBSERVED,
        "source_id": "synthetic-fixture",
        "data_origin": DataOrigin.SYNTHETIC,
    }


def portfolio(**changes):
    values = provenance() | {
        "equity": 100000,
        "peak_equity": 100000,
        "realised_day_pnl": 0,
        "unrealised_day_pnl": 0,
        "available_margin": 100000,
        "reserved_risk": 0,
        "exposures": (),
        "open_and_pending_positions": 0,
        "strategy_open_and_pending_positions": 0,
        "strategy_id": "test-strategy",
        "entries_blocked": False,
        "drawdown_disarmed": False,
    }
    values.update(changes)
    return PortfolioState(**values)


def market(**changes):
    values = provenance() | {
        "as_of": OBSERVED,
        "instrument_id": "test-equity",
        "bids": ({"price": "99.95", "quantity": 1000},),
        "asks": ({"price": "100.05", "quantity": 1000},),
        "ban_listed": False,
        "news_halt": False,
        "event_blackout": False,
        "manually_blocked": False,
        "mode": "PAPER",
    }
    values.update(changes)
    return MarketState(**values)


def verdict(*, proposed=None, account=None, prices=None, config=None):
    return evaluate(
        proposed or proposal(), account or portfolio(), prices or market(), config or limits()
    )


def test_risk_engine_purity(monkeypatch):
    expected = verdict()
    assert expected.approved and expected.approved_quantity == 100
    assert expected.risk_amount == 200

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O attempted inside risk evaluation")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        assert verdict().model_dump_json() == expected.model_dump_json()


def test_rule_registry_coverage():
    assert {rule.__name__ for rule in rules.RULES} == {
        "option_premium",
        "freshness",
        "provenance",
        "per_trade",
        "exposure",
        "daily_loss",
        "drawdown",
        "concentration",
        "position_count",
        "stop_loss",
        "reward_risk",
        "margin",
        "liquidity",
        "blocks",
    }
    decision = verdict()
    assert len({check.rule for check in decision.rules}) == len(decision.rules)
    assert all(check.passed and check.inputs for check in decision.rules)
    assert len(verdict(proposed=proposal(stop=None)).rules) == len(decision.rules)


def test_per_trade_risk_limit():
    assert verdict(proposed=proposal(quantity=250)).approved
    rejected = verdict(proposed=proposal(quantity=251))
    assert rejected.rejection_code == "PER_TRADE_RISK_EXCEEDED"
    assert rejected.risk_amount == 502
    assert rejected.approved_quantity == 0


def test_max_exposure_limit():
    existing = {
        "instrument_id": "other",
        "sector": "OTHER",
        "underlying": "OTHER",
        "notional": 290001,
    }
    rejected = verdict(account=portfolio(exposures=(existing,)))
    assert rejected.rejection_code == "MAX_EXPOSURE_EXCEEDED"
    existing["notional"] = 290000
    assert verdict(account=portfolio(exposures=(existing,))).approved


def test_daily_loss_limit_blocks_entries():
    rejected = verdict(account=portfolio(realised_day_pnl=-1000, unrealised_day_pnl=-1000))
    assert rejected.rejection_code == "DAILY_LOSS_LIMIT"
    assert verdict(account=portfolio(realised_day_pnl=-1800)).approved
    assert verdict(account=portfolio(realised_day_pnl=-1801)).rejection_code == "DAILY_LOSS_LIMIT"
    assert verdict(account=portfolio(reserved_risk=1801)).rejection_code == "DAILY_LOSS_LIMIT"
    assert verdict(account=portfolio(entries_blocked=True)).rejection_code == "DAILY_LOSS_LIMIT"


def test_max_drawdown_disarm():
    assert verdict(account=portfolio(equity=90000)).rejection_code == "MAX_DRAWDOWN_DISARM"
    assert verdict(account=portfolio(equity=90001)).approved
    assert (
        verdict(account=portfolio(drawdown_disarmed=True)).rejection_code == "MAX_DRAWDOWN_DISARM"
    )


@pytest.mark.parametrize("dimension", ["instrument", "sector", "underlying"])
def test_concentration_limits(dimension):
    rejected = verdict(config=limits(**{f"max_{dimension}_exposure_pct": 9}))
    assert rejected.rejection_code == f"{dimension.upper()}_CONCENTRATION_EXCEEDED"
    assert verdict(config=limits(**{f"max_{dimension}_exposure_pct": 10})).approved


def test_max_open_positions():
    assert (
        verdict(account=portfolio(open_and_pending_positions=5)).rejection_code == "MAX_POSITIONS"
    )
    assert (
        verdict(
            account=portfolio(open_and_pending_positions=2, strategy_open_and_pending_positions=2)
        ).rejection_code
        == "MAX_STRATEGY_POSITIONS"
    )


@pytest.mark.parametrize("stop", [None, 100, 101, Decimal("99.95"), 80])
def test_mandatory_stop_loss(stop):
    decision = verdict(proposed=proposal(stop=stop))
    assert not decision.approved
    assert not next(check for check in decision.rules if check.rule == "stop_loss").passed


def test_min_reward_risk():
    assert verdict(proposed=proposal(first_target=103)).approved
    assert verdict(proposed=proposal(first_target="102.95")).rejection_code == "MIN_REWARD_RISK"
    assert verdict(proposed=proposal(first_target=None)).rejection_code == "MIN_REWARD_RISK"


def test_margin_sufficiency():
    assert verdict(account=portfolio(available_margin=12500)).approved
    assert (
        verdict(account=portfolio(available_margin=12499)).rejection_code == "INSUFFICIENT_MARGIN"
    )


def test_slippage_limit():
    rejected = verdict(prices=market(asks=({"price": "100.5", "quantity": 1000},)))
    assert rejected.rejection_code == "SLIPPAGE_OR_DEPTH_LIMIT"
    assert (
        next(check for check in rejected.rules if check.rule == "liquidity").inputs[
            "slippage_per_unit"
        ]
        == "0.5"
    )
    assert verdict(prices=market(asks=({"price": "100.2", "quantity": 100},))).approved
    assert (
        verdict(prices=market(asks=({"price": "100.05", "quantity": 99},))).rejection_code
        == "SLIPPAGE_OR_DEPTH_LIMIT"
    )
    levels = ({"price": "100.05", "quantity": 50}, {"price": "100.35", "quantity": 50})
    assert verdict(prices=market(asks=levels)).approved


def test_risk_rejects_stale_inputs():
    stale = OBSERVED - timedelta(seconds=61)
    assert verdict(prices=market(observed_at=stale)).rejection_code == "STALE_MARKET_DATA"
    assert verdict(account=portfolio(observed_at=stale)).rejection_code == "STALE_PORTFOLIO"
    assert verdict(proposed=proposal(is_option=True)).rejection_code == "STALE_GREEKS"
    assert (
        verdict(proposed=proposal(generated_at=OBSERVED + timedelta(seconds=1))).rejection_code
        == "STALE_PROPOSAL"
    )
    assert (
        verdict(prices=market(available_at=OBSERVED + timedelta(seconds=1))).rejection_code
        == "STALE_MARKET_DATA"
    )


@pytest.mark.parametrize("block", ["ban_listed", "news_halt", "event_blackout", "manually_blocked"])
def test_instrument_blocks(block):
    assert verdict(prices=market(**{block: True})).rejection_code == block.upper()


def test_limit_forms_stricter_binds():
    assert (
        verdict(config=limits(max_per_trade_risk_amount=199)).rejection_code
        == "PER_TRADE_RISK_EXCEEDED"
    )
    assert verdict(config=limits(max_daily_loss_amount=199)).rejection_code == "DAILY_LOSS_LIMIT"
    assert (
        verdict(config=limits(max_gross_exposure_amount=9999)).rejection_code
        == "MAX_EXPOSURE_EXCEEDED"
    )
    assert (
        verdict(
            proposed=proposal(quantity=251), config=limits(max_per_trade_risk_amount=1000)
        ).rejection_code
        == "PER_TRADE_RISK_EXCEEDED"
    )


def test_risk_fail_closed(monkeypatch):
    def broken(*args):
        raise RuntimeError("injected")

    monkeypatch.setattr(engine, "RULES", (broken,))
    assert verdict().rejection_code == "RISK_ENGINE_ERROR"
    assert not evaluate(proposal(), portfolio(), market(), None).approved


def test_invalid_provenance_identity_and_immutable_limits():
    assert verdict(prices=market(mode="LIVE")).rejection_code == "INVALID_DATA_ORIGIN"
    assert verdict(prices=market(instrument_id="other")).rejection_code == "STATE_IDENTITY_MISMATCH"
    with pytest.raises(ValidationError):
        limits().capital = 999999
    with pytest.raises(ValidationError):
        limits(override_risk=True)


def test_lot_tick_and_option_risk():
    assert verdict(proposed=proposal(lot_size=75)).rejection_code == "INVALID_LOT_OR_TICK"
    assert verdict(proposed=proposal(entry="100.01")).rejection_code == "INVALID_LOT_OR_TICK"
    assert (
        verdict(
            proposed=proposal(is_option=True), prices=market(greeks=provenance())
        ).rejection_code
        == "UNDEFINED_OPTION_RISK"
    )


def test_absolute_drawdown_and_concentration():
    assert (
        verdict(
            account=portfolio(equity=99000), config=limits(max_drawdown_amount=1000)
        ).rejection_code
        == "MAX_DRAWDOWN_DISARM"
    )
    for dimension in ("instrument", "sector", "underlying"):
        assert (
            verdict(config=limits(**{f"max_{dimension}_exposure_amount": 9999})).rejection_code
            == f"{dimension.upper()}_CONCENTRATION_EXCEEDED"
        )


def test_short_liquidity_and_greek_freshness():
    assert verdict(proposed=proposal(direction="SHORT", stop=102, first_target=96)).approved
    assert (
        verdict(
            proposed=proposal(direction="SHORT", stop=102, first_target=96),
            prices=market(bids=({"price": "99.75", "quantity": 100},)),
        ).rejection_code
        == "SLIPPAGE_OR_DEPTH_LIMIT"
    )
    greek = provenance() | {"observed_at": OBSERVED - timedelta(seconds=61)}
    assert (
        verdict(proposed=proposal(is_option=True), prices=market(greeks=greek)).rejection_code
        == "STALE_GREEKS"
    )


def test_option_premium_bounds_and_naked_short_veto_are_independent():
    prices = market(greeks=provenance())
    base = {"is_option": True, "quantity": 1, "defined_max_loss_per_unit": 100, "first_target": 300}
    assert verdict(proposed=proposal(**base), prices=prices).approved
    for field in ("defined_max_loss_per_unit", "margin_per_unit", "exposure_per_unit"):
        assert (
            verdict(
                proposed=proposal(**(base | {field: Decimal("99.95")})), prices=prices
            ).rejection_code
            == "OPTION_PREMIUM_UNDERSTATED"
        )
    assert (
        verdict(
            proposed=proposal(
                **(
                    base
                    | {
                        "direction": "SHORT",
                        "stop": 102,
                        "first_target": 1,
                    }
                )
            ),
            prices=prices,
        ).rejection_code
        == "NAKED_SHORT_DISABLED"
    )
