"""Hand-computed synthetic sizing fixtures; never execution or profit evidence."""

from datetime import timedelta
from decimal import Decimal
from itertools import product

import pytest
from pydantic import ValidationError

from app.core.data_origin import DataOrigin
from app.core.enums import SignalDirection
from app.sizing.lots import protective_ticks, round_lots
from app.sizing.models import SizingInputs, SizingPolicy
from app.sizing.risk_based import size_position
from tests.unit.test_option_chain import OBSERVED


def policy(**changes):
    values = {
        "configured_capital": 100000,
        "per_trade_risk_pct": "0.5",
        "daily_loss_limit_pct": 2,
        "max_gross_exposure_multiple": 3,
        "max_instrument_exposure_pct": 100,
        "margin_buffer_pct": 20,
        "max_age_seconds": 60,
    }
    values.update(changes)
    return SizingPolicy(**values)


def evidence(**changes):
    values = {
        "proposal_id": "prp-synthetic",
        "instrument_id": "ins-synthetic",
        "as_of": OBSERVED,
        "observed_at": OBSERVED,
        "available_at": OBSERVED,
        "source_ids": ("test-fixture",),
        "data_origin": DataOrigin.SYNTHETIC,
        "equity": 100000,
        "available_margin": 100000,
        "gross_exposure": 0,
        "instrument_exposure": 0,
        "daily_loss": 0,
        "reserved_risk": 0,
        "entry": 100,
        "stop": 98,
        "direction": SignalDirection.LONG,
        "lot_size": 1,
        "tick_size": "0.05",
        "margin_per_unit": 100,
        "exposure_per_unit": 100,
        "risk_cost_per_unit": 0,
        "requested_risk_pct": 1,
    }
    values.update(changes)
    return SizingInputs(**values)


def test_risk_based_sizing():
    result = size_position(evidence(), policy())
    assert result.quantity == 250
    assert result.risk_budget == 500
    assert result.allocated_risk == 500
    assert result.binding_constraint == "RISK"
    assert result == size_position(evidence(), policy())
    assert size_position(evidence(requested_risk_pct="0.1"), policy()).quantity == 50
    assert size_position(evidence(equity=50000), policy()).quantity == 125
    assert size_position(evidence(risk_cost_per_unit="0.5"), policy()).quantity == 200


def test_lot_rounding_down():
    assert round_lots(Decimal(95), 50) == 50
    assert size_position(evidence(lot_size=75), policy()).quantity == 225
    assert protective_ticks(
        Decimal("100.01"), Decimal("98.04"), Decimal("0.05"), SignalDirection.LONG
    ) == (Decimal("100.05"), Decimal("98.00"))
    assert protective_ticks(
        Decimal("100.04"), Decimal("102.01"), Decimal("0.05"), SignalDirection.SHORT
    ) == (Decimal("100.00"), Decimal("102.05"))
    assert size_position(evidence(entry="100.01", stop="98.04"), policy()).quantity == 243


def test_defined_loss_basis_sizes_against_full_budget_not_only_stop_distance():
    result = size_position(evidence(defined_max_loss_per_unit=100, lot_size=5), policy())
    assert result.quantity == 5
    assert result.risk_per_unit == 100 and result.allocated_risk == 500
    assert result.formula_version == "1.1.0"
    charged = size_position(evidence(defined_max_loss_per_unit=100, risk_cost_per_unit=1), policy())
    assert charged.quantity == 4 and charged.allocated_risk == 404
    assert size_position(evidence(defined_max_loss_per_unit=1), policy()).quantity == 250
    too_large = size_position(evidence(defined_max_loss_per_unit=100, lot_size=25), policy())
    assert too_large.quantity == 0 and too_large.zero_reason == "BUDGET_BELOW_MIN_LOT"


def test_zero_size_no_trade():
    result = size_position(evidence(lot_size=300), policy())
    assert result.quantity == 0 and result.allocated_risk == 0
    assert result.zero_reason == "BUDGET_BELOW_MIN_LOT"
    assert size_position(evidence(equity=0), policy()).quantity == 0


def test_margin_constrained_sizing():
    result = size_position(evidence(available_margin=10000, lot_size=50), policy())
    assert result.quantity == 50
    assert result.caps["MARGIN"] == 80
    assert result.binding_constraint == "MARGIN"
    assert size_position(evidence(), policy(margin_buffer_pct=100)).quantity == 0


def test_volatility_adjusted_sizing():
    assert size_position(evidence(atr=4, reference_atr=2), policy()).quantity == 125
    assert size_position(evidence(atr=2, reference_atr=2), policy()).quantity == 250
    assert size_position(evidence(atr=1, reference_atr=2), policy()).quantity == 250


def test_exposure_aware_sizing():
    gross = size_position(evidence(gross_exposure=290000), policy())
    assert gross.quantity == 100 and gross.binding_constraint == "GROSS_EXPOSURE"
    concentration = size_position(
        evidence(gross_exposure=9500, instrument_exposure=9500),
        policy(max_instrument_exposure_pct=10),
    )
    assert concentration.quantity == 5 and concentration.binding_constraint == "CONCENTRATION"
    assert size_position(evidence(gross_exposure=300001), policy()).quantity == 0


def test_daily_budget_sizing():
    result = size_position(evidence(daily_loss=1600), policy())
    assert result.risk_budget == 400 and result.quantity == 200
    assert result.binding_constraint == "DAILY_BUDGET"
    assert size_position(evidence(daily_loss=1600, reserved_risk=300), policy()).quantity == 50
    assert size_position(evidence(daily_loss=2001), policy()).quantity == 0


def test_kelly_capped_and_off_by_default():
    assert policy().kelly_enabled is False
    assert size_position(evidence(win_probability=0, payoff_ratio=1), policy()).quantity == 250
    enabled = policy(kelly_enabled=True, kelly_fraction_cap="0.001")
    assert size_position(evidence(win_probability="0.6", payoff_ratio=2), enabled).quantity == 50
    assert size_position(evidence(win_probability="0.2", payoff_ratio=1), enabled).quantity == 0
    with pytest.raises(ValueError, match="EVIDENCE_UNAVAILABLE"):
        size_position(evidence(), enabled)
    with pytest.raises(ValueError, match="fraction cap"):
        policy(kelly_enabled=True)


def test_missing_capital_not_invented(settings_env):
    settings = settings_env()
    configured = SizingPolicy.from_settings(
        settings, max_instrument_exposure_pct=10, max_age_seconds=60
    )
    assert configured.configured_capital is None
    with pytest.raises(ValueError, match="STARTING_CAPITAL_UNAVAILABLE"):
        size_position(evidence(), configured)
    settings = settings_env(starting_capital=100000, per_trade_risk_pct="0.1")
    configured = SizingPolicy.from_settings(
        settings, max_instrument_exposure_pct=100, max_age_seconds=60
    )
    assert size_position(evidence(), configured).quantity == 50


@pytest.mark.parametrize(
    "change",
    [
        {"entry": "NaN"},
        {"stop": 100},
        {"lot_size": 0},
        {"lot_size": True},
        {"tick_size": 0},
        {"margin_per_unit": 0},
        {"source_ids": ()},
        {"atr": 2},
        {"daily_loss": -1},
        {"instrument_exposure": 1},
    ],
)
def test_invalid_sizing_evidence(change):
    with pytest.raises(ValidationError):
        evidence(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": OBSERVED - timedelta(seconds=61)},
        {"available_at": OBSERVED + timedelta(seconds=1)},
    ],
)
def test_sizing_no_lookahead_or_stale_state(change):
    with pytest.raises(ValueError, match="STALE_OR_FUTURE"):
        size_position(evidence(**change), policy())


@pytest.mark.parametrize("quantity,lot", [(Decimal(-1), 1), (Decimal(1), 0), (Decimal("NaN"), 1)])
def test_invalid_lot_rounding(quantity, lot):
    with pytest.raises(ValueError):
        round_lots(quantity, lot)


def test_sizing_never_exceeds_any_cap():
    cases = product(
        (1, 25, 75, 200), (0, 1, 10000, 100000), (0, 1600, 2000, 2500), (0, 290000, 300000, 350000)
    )
    for lot, margin, loss, exposure in cases:
        result = size_position(
            evidence(
                lot_size=lot, available_margin=margin, daily_loss=loss, gross_exposure=exposure
            ),
            policy(),
        )
        assert result.quantity % lot == 0
        assert result.quantity * 2 <= min(500, max(0, 2000 - loss))
        assert result.quantity * 100 <= Decimal(margin) * Decimal("0.8")
        assert result.quantity * 100 <= max(0, 300000 - exposure)
        assert result.quantity * 100 <= 100000
