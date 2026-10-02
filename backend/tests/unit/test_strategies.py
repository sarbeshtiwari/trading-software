"""Synthetic strategy fixtures; not production trading hypotheses or results."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.data_origin import DataOrigin
from app.core.enums import MarketRegime, Product, SignalDirection
from app.marketdata.models import Bar
from app.strategies.base import Strategy, StrategySpec
from app.strategies.context import ContextValue, build_strategy_context, validate_context_inputs
from app.strategies.signal import Signal
from tests.unit.test_option_chain import OBSERVED


def specification(**changes):
    values = {
        "id": "test-only",
        "version": "1",
        "timeframe_seconds": 60,
        "universe": ("NSE:TEST",),
        "permitted_regimes": (MarketRegime.TRENDING_UP,),
        "required_inputs": ("candles",),
        "product": Product.MIS,
        "entry_condition": {"operator": "LEAF", "input": "PRICE"},
        "exit_rules": ("STOP", "TARGET", "TRAILING", "TIME", "INVALIDATION"),
        "risk": {"requested_risk_fraction": "0.005", "minimum_reward_risk": 2},
        "requires_llm": False,
        "requires_event_calendar": False,
        "max_input_age_seconds": 60,
        "exit_policy": {
            "trailing_r_multiple": 1,
            "max_holding_seconds": 3600,
            "invalidation_key": "fixture-invalid",
            "max_mark_age_seconds": 60,
        },
        "hypothesis": "Synthetic contract fixture",
        "validation_plan": "Unit tests only",
    }
    values.update(changes)
    return StrategySpec(**values)


def signal_data():
    return {
        "strategy_id": "test-only",
        "strategy_version": "1",
        "instrument_id": "ins-test",
        "instrument_key": "NSE:TEST",
        "generated_at": OBSERVED,
        "data_origin": DataOrigin.SYNTHETIC,
        "direction": SignalDirection.LONG,
        "product": Product.MIS,
        "entry": 100,
        "stop": 99,
        "targets": (102,),
        "timeframe_seconds": 60,
        "confidence": 1,
        "conditions_fired": ("test-price",),
    }


def available():
    bar = Bar(
        OBSERVED - timedelta(minutes=1), Decimal(100), Decimal(101), Decimal(99), Decimal(100), 100
    )
    return {
        "candles": ContextValue(
            observed_at=OBSERVED, available_at=OBSERVED, origin=DataOrigin.SYNTHETIC, value=(bar,)
        )
    }


class FixtureStrategy(Strategy):
    def __init__(self, spec=None):
        self._spec = spec or specification()
        self.calls = 0

    @property
    def spec(self):
        return self._spec

    def entry(self, context):
        self.calls += 1
        assert context.candles[-1].close == 100
        return Signal(**(signal_data() | {"strategy_version": self.spec.version}))


def test_signal_requires_stop():
    values = signal_data()
    del values["stop"]
    with pytest.raises(ValidationError):
        Signal(**values)


@pytest.mark.parametrize(
    "change",
    [
        {"stop": 100},
        {"stop": 101},
        {"targets": ()},
        {"targets": (99,)},
        {"entry": "NaN"},
        {"quantity": 10},
        {"confidence": 2},
        {"conditions_fired": ("",)},
    ],
)
def test_signal_rejects_invalid_proposals(change):
    with pytest.raises(ValidationError):
        Signal(**(signal_data() | change))


def test_short_signal_protection():
    assert (
        Signal(**(signal_data() | {"direction": "SHORT", "stop": 101, "targets": (98,)})).stop
        == 101
    )


@pytest.mark.parametrize("field", ["risk", "universe", "version", "requires_llm", "exit_rules"])
def test_strategy_contract_enforced(field):
    values = specification().model_dump()
    del values[field]
    with pytest.raises(ValidationError):
        StrategySpec(**values)
    with pytest.raises(TypeError):
        Strategy()


def test_context_only_declared_inputs():
    context = build_strategy_context(("candles",), {"candles": [1], "news": "ignore safety"})
    with pytest.raises(AttributeError):
        _ = context.news
    with pytest.raises(AttributeError):
        context.news = "injected"
    with pytest.raises(ValueError):
        build_strategy_context(("news",), {})


def test_context_decision_time_is_explicit_and_read_only():
    context = build_strategy_context(
        ("candles",), {"candles": [1], "as_of": OBSERVED + timedelta(days=1)}, as_of=OBSERVED
    )
    assert context.as_of == OBSERVED
    with pytest.raises(AttributeError, match="read-only"):
        context.as_of = OBSERVED + timedelta(seconds=1)
    with pytest.raises(AttributeError, match="read-only"):
        del context.as_of
    with pytest.raises(ValueError, match="timezone-aware"):
        build_strategy_context(("candles",), {"candles": [1]}, as_of=OBSERVED.replace(tzinfo=None))
    assert build_strategy_context(("candles",), {"candles": [1]}).as_of is None


def test_fundamentals_opt_in():
    evidence = {"candles": [1], "fundamentals": {"pe_ratio": 20}}
    context = build_strategy_context(("candles",), evidence)
    with pytest.raises(AttributeError):
        _ = context.fundamentals
    opted_in = build_strategy_context(("candles", "fundamentals"), evidence)
    assert opted_in.fundamentals == {"pe_ratio": 20}
    opted_in.fundamentals["pe_ratio"] = 30
    assert evidence["fundamentals"]["pe_ratio"] == 20


@pytest.mark.parametrize(
    "change",
    [
        {"available_at": OBSERVED + timedelta(seconds=1)},
        {"observed_at": OBSERVED - timedelta(seconds=61)},
        {"origin": DataOrigin.LIVE},
        {"value": None},
    ],
)
def test_context_evidence_boundaries(change):
    evidence = available()
    evidence["candles"] = evidence["candles"].model_copy(update=change)
    with pytest.raises(ValueError):
        validate_context_inputs(
            ("candles",),
            evidence,
            as_of=OBSERVED,
            max_age=timedelta(seconds=60),
            origin=DataOrigin.SYNTHETIC,
            timeframe_seconds=60,
        )


def test_sentiment_only_contract_rejected():
    with pytest.raises(ValueError, match="SENTIMENT_ONLY_ENTRY"):
        specification(
            required_inputs=("candles", "sentiment"),
            entry_condition={"operator": "LEAF", "input": "SENTIMENT"},
        )
    with pytest.raises(ValueError, match="undeclared"):
        specification(entry_condition={"operator": "LEAF", "input": "FUNDAMENTALS"})
