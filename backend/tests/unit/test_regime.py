"""Labelled synthetic fixtures for deterministic regimes and conservative transitions."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis.regime.classifier import RegimePolicy, classify
from app.analysis.regime.events import EventCalendar, ScheduledEvent
from app.analysis.regime.inputs import IndicatorPolicy, Observation, RegimeInputs, build_inputs
from app.core.data_origin import DataOrigin
from app.core.enums import MarketRegime
from app.marketdata.models import Bar
from tests.unit.test_option_chain import OBSERVED


def observation(value, at=OBSERVED):
    return Observation(
        value=Decimal(value), observed_at=at, available_at=at, source="synthetic-test"
    )


def inputs(at=OBSERVED, **changes):
    values = {
        "adx": 30,
        "ma_structure": 1,
        "realised_volatility": 20,
        "implied_volatility": 20,
        "breadth": Decimal("0.6"),
        "event_risk": 0,
    }
    values.update(changes)
    return RegimeInputs(
        underlying="TEST",
        as_of=at,
        data_origin=DataOrigin.SYNTHETIC,
        **{
            name: observation(value, at) if value is not None else None
            for name, value in values.items()
        },
        event_ids=("event-test",) if values["event_risk"] else (),
    )


def policy():
    return RegimePolicy(
        adx_trending=Decimal(25),
        high_volatility=Decimal(30),
        low_volatility=Decimal(10),
        bullish_breadth=Decimal("0.55"),
        bearish_breadth=Decimal("0.45"),
        confirmations=2,
        max_changes_per_session=2,
        max_age_seconds=60,
    )


@pytest.mark.parametrize(
    "changes,label",
    [
        ({}, MarketRegime.TRENDING_UP),
        ({"ma_structure": -1, "breadth": Decimal("0.4")}, MarketRegime.TRENDING_DOWN),
        ({"adx": 20}, MarketRegime.RANGING),
        ({"implied_volatility": 30}, MarketRegime.HIGH_VOLATILITY),
        ({"realised_volatility": 10, "implied_volatility": 10}, MarketRegime.LOW_VOLATILITY),
        ({"event_risk": 1}, MarketRegime.EVENT_RISK),
        ({"breadth": None}, MarketRegime.UNKNOWN),
        ({"event_risk": None}, MarketRegime.UNKNOWN),
    ],
)
def test_regime_classifier(changes, label):
    result = classify(inputs(**changes), policy())
    assert result.label == label
    assert result == classify(inputs(**changes), policy())


def test_regime_inputs_recorded():
    snapshot = inputs()
    result = classify(snapshot, policy())
    assert result.inputs == snapshot
    assert result.inputs.breadth.value == Decimal("0.6")
    assert result.policy == policy()
    assert type(result).model_validate_json(result.model_dump_json()) == result


def test_regime_hysteresis():
    state = classify(inputs(adx=24), policy())
    labels = []
    for index in range(1, 21):
        state = classify(
            inputs(OBSERVED + timedelta(seconds=index), adx=26 if index % 2 else 24),
            policy(),
            state,
        )
        labels.append(state.label)
    assert set(labels) == {MarketRegime.RANGING}
    for index, level in enumerate((26, 26, 24, 24, 26, 26), start=21):
        state = classify(inputs(OBSERVED + timedelta(seconds=index), adx=level), policy(), state)
    assert state.changes == 2
    assert state.label == MarketRegime.RANGING
    assert state.reason == "SESSION_CHANGE_CAP"


def test_safety_overrides_hysteresis_cap_and_recovers_with_confirmation():
    state = classify(inputs(), policy())
    state = classify(inputs(OBSERVED + timedelta(seconds=1), event_risk=1), policy(), state)
    assert state.label == MarketRegime.EVENT_RISK
    state = classify(inputs(OBSERVED + timedelta(seconds=2)), policy(), state)
    assert state.label == MarketRegime.EVENT_RISK
    state = classify(inputs(OBSERVED + timedelta(seconds=3)), policy(), state)
    assert state.label == MarketRegime.TRENDING_UP
    state = classify(inputs(OBSERVED + timedelta(seconds=4), breadth=None), policy(), state)
    assert state.label == MarketRegime.UNKNOWN


def test_session_reset_and_chronology():
    state = classify(inputs(), policy())
    with pytest.raises(ValueError, match="advance"):
        classify(inputs(), policy(), state)
    tomorrow = classify(inputs(OBSERVED + timedelta(days=1), adx=20), policy(), state)
    assert tomorrow.label == MarketRegime.RANGING
    assert tomorrow.changes == 0


def test_stale_and_future_inputs():
    snapshot = inputs()
    stale = snapshot.model_copy(
        update={"breadth": observation("0.6", OBSERVED - timedelta(seconds=61))}
    )
    assert classify(stale, policy()).label == MarketRegime.UNKNOWN
    with pytest.raises(ValidationError):
        RegimeInputs.model_validate(
            {
                **snapshot.model_dump(),
                "breadth": observation("0.6", OBSERVED + timedelta(seconds=1)),
            }
        )
    with pytest.raises(ValidationError):
        inputs(adx=101)
    with pytest.raises(ValidationError):
        inputs(breadth=Decimal("NaN"))


def calendar():
    event = ScheduledEvent(
        id="event-test",
        source="test-exchange",
        known_at=OBSERVED - timedelta(days=1),
        start=OBSERVED,
        end=OBSERVED + timedelta(minutes=5),
        high_impact=True,
    )
    return EventCalendar(
        source="test-exchange",
        known_at=event.known_at,
        coverage_start=OBSERVED - timedelta(days=1),
        coverage_end=OBSERVED + timedelta(days=1),
        events=(event,),
    )


def test_event_risk_regime():
    supplied = calendar()
    assert supplied.active("TEST", OBSERVED) == ("event-test",)
    assert supplied.active("TEST", OBSERVED - timedelta(seconds=1)) == ()
    assert supplied.active("TEST", OBSERVED - timedelta(days=2)) is None
    late = supplied.model_copy(update={"known_at": OBSERVED + timedelta(seconds=1)})
    assert late.active("TEST", OBSERVED) is None


def test_indicator_builder_reuses_closed_bar_indicators():
    settings = IndicatorPolicy(
        adx_period=2,
        fast_period=2,
        slow_period=3,
        volatility_period=2,
        periods_per_year=252,
        bar_seconds=60,
    )
    bars = [
        Bar(
            ts=OBSERVED - timedelta(minutes=6 - index),
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=100,
        )
        for index in range(6)
    ]
    arguments = {
        "underlying": "TEST",
        "as_of": OBSERVED,
        "bars_available_at": OBSERVED,
        "source": "synthetic-test",
        "data_origin": DataOrigin.SYNTHETIC,
        "policy": settings,
        "implied_volatility": observation(20),
        "breadth": observation("0.6"),
        "calendar": calendar(),
    }
    result = build_inputs(bars, **arguments)
    assert result.adx.value == 100
    assert result.ma_structure.value == 1
    assert result.realised_volatility.value >= 0
    assert result.event_risk.value == 1
    assert classify(result, policy()).label == MarketRegime.EVENT_RISK
    assert build_inputs(bars[:1], **arguments).adx is None
    with pytest.raises(ValueError, match="future"):
        build_inputs(
            [
                *bars,
                Bar(
                    ts=OBSERVED, open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1)
                ),
            ],
            **arguments,
        )
