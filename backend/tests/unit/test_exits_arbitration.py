"""Synthetic mark sequences and deterministic signal-conflict policy."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis.equity import PriceObservation
from app.strategies.arbitration import resolve_conflicts
from app.strategies.base import StrategySpec
from app.strategies.exits import ExitContext, ExitPolicy, ExitState, Invalidation, evaluate_exit
from app.strategies.registry import StrategyRegistry
from app.strategies.signal import Signal
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_strategies import FixtureStrategy, signal_data, specification


def state(**changes):
    return ExitState(
        **(
            {
                "position_id": "pos-fixture",
                "direction": "LONG",
                "entry": 100,
                "stop": 98,
                "target": 110,
                "favourable_price": 100,
                "opened_at": OBSERVED - timedelta(minutes=10),
                "updated_at": OBSERVED - timedelta(seconds=1),
            }
            | changes
        )
    )


def check(value, *, position=None, active=False, at=OBSERVED):
    return evaluate_exit(
        position or state(),
        PriceObservation(value=value, observed_at=at, available_at=at),
        Invalidation(key="fixture-invalid", active=active, observed_at=at, available_at=at),
        specification().exit_policy,
        as_of=at,
    )


@pytest.mark.parametrize(
    "field",
    ["trailing_r_multiple", "max_holding_seconds", "invalidation_key", "max_mark_age_seconds"],
)
def test_exit_completeness(field):
    data = specification().exit_policy.model_dump()
    del data[field]
    with pytest.raises(ValidationError):
        ExitPolicy(**data)
    spec = specification().model_dump()
    del spec["exit_policy"]
    with pytest.raises(ValidationError):
        StrategySpec(**spec)


@pytest.mark.parametrize(
    "price,reason", [(97, "STOP"), (98, "STOP"), (110, "TARGET"), (111, "TARGET"), (100, None)]
)
def test_stop_and_target_boundaries(price, reason):
    assert check(price).reason == reason


def test_trailing_time_and_invalidation():
    first = check(106)
    assert first.reason is None and first.trailing_stop == 104
    assert check(104, position=first.state, at=OBSERVED + timedelta(seconds=1)).reason == "TRAILING"
    assert check(100, position=state(opened_at=OBSERVED - timedelta(hours=1))).reason == "TIME"
    assert check(100, active=True).reason == "INVALIDATION"


def test_absent_invalidation_is_not_a_fabricated_hold_decision():
    result = evaluate_exit(
        state(),
        PriceObservation(value=106, observed_at=OBSERVED, available_at=OBSERVED),
        None,
        specification().exit_policy,
        as_of=OBSERVED,
    )
    assert result.reason is None and not result.invalidation_checked
    assert result.state.favourable_price == 106 and result.trailing_stop == 104
    assert check(98, active=True).reason == "STOP"


def test_short_exits_and_no_lookahead():
    short = state(direction="SHORT", stop=102, target=90)
    assert check(102, position=short).reason == "STOP"
    assert check(90, position=short).reason == "TARGET"
    moved = check(94, position=short)
    assert moved.trailing_stop == 96
    assert check(96, position=moved.state, at=OBSERVED + timedelta(seconds=1)).reason == "TRAILING"
    with pytest.raises(ValueError):
        check(100, position=state(updated_at=OBSERVED + timedelta(seconds=1)))
    with pytest.raises(ValueError, match="stale"):
        evaluate_exit(
            state(),
            PriceObservation(value=100, observed_at=OBSERVED, available_at=OBSERVED),
            Invalidation(
                key="fixture-invalid", active=False, observed_at=OBSERVED, available_at=OBSERVED
            ),
            specification().exit_policy,
            as_of=OBSERVED + timedelta(seconds=61),
        )


def candidates():
    long = Signal(**(signal_data() | {"strategy_id": "alpha"}))
    short = Signal(
        **(
            signal_data()
            | {"strategy_id": "beta", "direction": "SHORT", "stop": 101, "targets": (98,)}
        )
    )
    return long, short


def test_signal_conflict_resolution(caplog):
    signals = candidates()
    with caplog.at_level("INFO"):
        result = resolve_conflicts(signals, {"alpha": 2, "beta": 1})
    assert result.winners == (signals[1],)
    assert result.suppressed[0].reason == "OPPOSING_SIGNAL"
    assert result == resolve_conflicts(tuple(reversed(signals)), {"alpha": 2, "beta": 1})
    assert any(
        getattr(record, "winner_strategy", None) == "beta"
        and getattr(record, "suppressed_strategy", None) == "alpha"
        for record in caplog.records
    )
    assert resolve_conflicts(signals, {"alpha": 1, "beta": 1}).winners == (signals[0],)


def test_arbitration_missing_priority_duplicates_and_future_signals():
    long, short = candidates()
    with pytest.raises(ValueError, match="priority"):
        resolve_conflicts((long, short), {"alpha": 1})
    with pytest.raises(ValueError, match="duplicate"):
        resolve_conflicts((long, long), {"alpha": 1})
    future = short.model_copy(update={"generated_at": OBSERVED + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="timestamp"):
        resolve_conflicts((long, future), {"alpha": 1, "beta": 2})


async def test_exit_evaluator_cannot_be_replaced():
    class UnsafeStrategy(FixtureStrategy):
        def exit(self, context, position):
            return None

    with pytest.raises(ValueError, match="cannot replace"):
        await StrategyRegistry().register(UnsafeStrategy())
    context = ExitContext(
        mark=PriceObservation(value=98, observed_at=OBSERVED, available_at=OBSERVED),
        invalidation=Invalidation(
            key="fixture-invalid", active=False, observed_at=OBSERVED, available_at=OBSERVED
        ),
        as_of=OBSERVED,
    )
    assert FixtureStrategy().exit(context, state()).reason == "STOP"
    assert (
        evaluate_exit(
            state(),
            context.mark,
            context.invalidation,
            specification().exit_policy.model_copy(update={"trailing_r_multiple": Decimal(100)}),
            as_of=OBSERVED,
        ).trailing_stop
        == 98
    )
