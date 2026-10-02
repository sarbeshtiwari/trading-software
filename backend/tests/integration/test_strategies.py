"""Persisted strategy gates using only explicitly synthetic test signals."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.analysis.events import EventBlackout
from app.analysis.regime.classifier import classify
from app.core.clock import FakeClock
from app.db import session as db_session
from app.db.models.config import StrategyRegistration
from app.main import create_app
from app.modes import TradingMode
from app.strategies.engine import StrategyEngine
from app.strategies.registry import StrategyRegistry
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_regime import inputs, policy
from tests.unit.test_strategies import FixtureStrategy, available, specification

pytestmark = pytest.mark.usefixtures("authenticated_api")


async def evaluate(strategy, **changes):
    values = {
        "instrument_id": "ins-test",
        "instrument_key": "NSE:TEST",
        "as_of": OBSERVED,
        "regime_id": "regime-test",
        "regime": classify(inputs(), policy()),
        "available": available(),
        "llm_available": False,
    }
    values.update(changes)
    return await StrategyEngine(StrategyRegistry(), FakeClock(OBSERVED)).evaluate(
        strategy, **values
    )


async def test_strategy_registry_mode_gating(db_engine):
    registry = StrategyRegistry()
    strategy = FixtureStrategy()
    identifier = await registry.register(strategy, enabled_paper=True)
    assert await registry.register(strategy) == identifier
    assert (await evaluate(strategy)).reason == "SIGNAL"
    assert (await evaluate(strategy, mode=TradingMode.LIVE)).reason == "STRATEGY_MODE_DISABLED"
    with pytest.raises(ValueError, match="NOT APPROVED"):
        await registry.set_enabled(strategy.spec.id, "1", TradingMode.LIVE, True)
    await registry.set_enabled(strategy.spec.id, "1", TradingMode.PAPER, False)
    assert (await evaluate(strategy)).signal is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/strategies")
    assert response.status_code == 200
    assert response.json()[0]["enabled_paper"] is False
    assert response.json()[0]["enabled_live"] is False


async def test_regime_gating(db_engine, caplog):
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    with caplog.at_level("INFO"):
        result = await evaluate(strategy, regime=classify(inputs(adx=20), policy()))
    assert result.reason == "REGIME_NOT_PERMITTED"
    assert result.signal is None and strategy.calls == 0
    assert any(getattr(record, "regime_id", None) == "regime-test" for record in caplog.records)


async def test_event_risk_suppression(db_engine):
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    assert (
        await evaluate(strategy, regime=classify(inputs(event_risk=1), policy()))
    ).reason == "REGIME_NOT_PERMITTED"
    blackout = EventBlackout(
        symbol="TEST",
        as_of=OBSERVED,
        status="BLACKOUT",
        event_ids=("results-test",),
        restricted_strategies=(strategy.spec.id,),
    )
    assert (await evaluate(strategy, blackout=blackout)).reason == "EVENT_BLACKOUT_OR_UNAVAILABLE"
    assert (
        await evaluate(strategy, blackout=blackout.model_copy(update={"restricted_strategies": ()}))
    ).reason == "SIGNAL"
    for changes in ({"as_of": OBSERVED + timedelta(seconds=1)}, {"symbol": "OTHER"}):
        assert (
            await evaluate(strategy, blackout=blackout.model_copy(update=changes))
        ).signal is None


async def test_required_calendar_missing(db_engine):
    strategy = FixtureStrategy(specification(requires_event_calendar=True))
    await StrategyRegistry().register(strategy, enabled_paper=True)
    assert (await evaluate(strategy)).reason == "EVENT_BLACKOUT_OR_UNAVAILABLE"


async def test_llm_dependency_degradation(db_engine):
    registry = StrategyRegistry()
    dependent = FixtureStrategy(specification(requires_llm=True))
    await registry.register(dependent, enabled_paper=True)
    assert (await evaluate(dependent)).reason == "LLM_UNAVAILABLE"
    deterministic = FixtureStrategy(specification(version="2"))
    await registry.register(deterministic, enabled_paper=True)
    result = await evaluate(deterministic)
    assert result.reason == "SIGNAL"
    assert deterministic.calls == 1


async def test_strategy_determinism(db_engine):
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    first = await evaluate(strategy)
    second = await evaluate(strategy)
    assert first.signal is not None
    assert first.model_dump_json() == second.model_dump_json()


async def test_signal_identity_mismatch_rejected(db_engine):
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    result = await evaluate(strategy, instrument_id="different-instrument")
    assert result.signal is None
    assert result.reason == "INVALID_OR_UNAVAILABLE_STRATEGY_INPUT_OUTPUT"


async def test_strategy_version_integrity(db_engine):
    registry = StrategyRegistry()
    await registry.register(FixtureStrategy(), enabled_paper=True)
    changed = FixtureStrategy(specification(max_input_age_seconds=120))
    with pytest.raises(ValueError, match="new version"):
        await registry.register(changed)
    assert (await evaluate(changed)).reason == "UNREGISTERED_OR_CHANGED_STRATEGY"
    with pytest.raises(ValueError, match="contract"):
        await registry.register(object())


async def test_auto_disable_and_live_tampering_fail_closed(db_engine):
    registry = StrategyRegistry()
    strategy = FixtureStrategy()
    identifier = await registry.register(strategy, enabled_paper=True)
    async with db_session.session_scope() as session:
        row = await session.get(StrategyRegistration, identifier)
        row.auto_disabled = True
        row.enabled_live = True
        row.live_approved = True
    assert (await evaluate(strategy)).signal is None
    assert (await evaluate(strategy, mode=TradingMode.LIVE)).signal is None


@pytest.mark.parametrize("offset", [0, -180])
async def test_future_or_stale_candles_rejected(db_engine, offset):
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    evidence = available()
    candle = replace(evidence["candles"].value[0], ts=OBSERVED + timedelta(seconds=offset))
    evidence["candles"] = evidence["candles"].model_copy(update={"value": (candle,)})
    result = await evaluate(strategy, available=evidence)
    assert result.signal is None and strategy.calls == 0


async def test_stale_future_and_provenance_gates(db_engine):
    strategy = FixtureStrategy()
    registry = StrategyRegistry()
    await registry.register(strategy, enabled_paper=True)
    await registry.set_enabled(strategy.spec.id, "1", TradingMode.SUPERVISED, True)
    assert (await evaluate(strategy, mode=TradingMode.SUPERVISED)).reason == "NONLIVE_DATA"
    assert (
        await evaluate(strategy, as_of=OBSERVED + timedelta(seconds=1))
    ).reason == "FUTURE_DECISION"
    assert (await evaluate(strategy, regime_id="")).reason == "MISSING_REGIME_ID"
    for offset in (-61, 1):
        regime = classify(inputs(OBSERVED + timedelta(seconds=offset)), policy())
        assert (await evaluate(strategy, regime=regime)).reason == "STALE_OR_FUTURE_REGIME"
