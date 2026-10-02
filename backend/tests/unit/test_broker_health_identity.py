from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import HealthStatus
from app.marketdata.live import LiveMarketDataProvider
from app.monitoring.broker_checks import GrowwAuthCheck, MarketDataCheck
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_option_chain import OBSERVED


@pytest.mark.parametrize("mode", ["PAPER", "SUPERVISED"])
@pytest.mark.parametrize(
    "name,realism",
    [
        ("paper", ExecutionRealism.SIMULATED),
        ("groww", ExecutionRealism.SIMULATED),
        ("other", ExecutionRealism.REAL),
    ],
)
async def test_simulated_or_wrong_adapter_cannot_verify_groww(settings_env, mode, name, realism):
    settings = settings_env(
        TRADING_MODE=mode,
        BROKER_PROVIDER="groww" if mode == "SUPERVISED" else "paper",
        GROWW_API_KEY="isolated-test-key",
        GROWW_API_SECRET="isolated-test-secret",
    )
    broker = SimpleNamespace(
        name=name, execution_realism=realism, ping=AsyncMock(return_value=True)
    )
    result = await GrowwAuthCheck(broker, settings).execute()
    assert result.status is (HealthStatus.FAIL if mode == "SUPERVISED" else HealthStatus.SKIPPED)
    assert "UNVERIFIED" in result.detail
    broker.ping.assert_not_awaited()


@pytest.mark.parametrize("reachable", [False, True])
async def test_read_only_probe_never_certifies_live_execution(settings_env, reachable):
    settings = settings_env(
        GROWW_API_KEY="isolated-test-key", GROWW_API_SECRET="isolated-test-secret"
    )
    broker = SimpleNamespace(
        name="groww",
        execution_realism=ExecutionRealism.REAL,
        ping=AsyncMock(return_value=reachable),
    )
    result = await GrowwAuthCheck(broker, settings).execute()
    broker.ping.assert_awaited_once()
    assert result.status is (HealthStatus.PASS if reachable else HealthStatus.FAIL)
    if reachable:
        assert "live execution UNVERIFIED" in result.detail


@pytest.mark.parametrize(
    "status,expected",
    [
        (HealthStatus.PASS, HealthStatus.PASS),
        (HealthStatus.FAIL, HealthStatus.FAIL),
        (HealthStatus.DEGRADED, HealthStatus.DEGRADED),
        (HealthStatus.SKIPPED, HealthStatus.SKIPPED),
        ("unexpected", HealthStatus.FAIL),
        (None, HealthStatus.SKIPPED),
    ],
)
async def test_market_health_never_invents_success(status, expected):
    provider = SimpleNamespace(name="isolated-fixture", data_origin=DataOrigin.SYNTHETIC)
    if status is not None:
        provider.status = status
    result = await MarketDataCheck(provider).execute()
    assert result.status is expected


@pytest.mark.parametrize("feed_state", ["none", "working", "failed"])
async def test_live_provider_health_requires_actual_fresh_observation(fake_clock, feed_state):
    fake_clock.set_to(OBSERVED)
    quote = replace(fixture_provider().get_quote.return_value, data_origin=DataOrigin.LIVE)
    api = SimpleNamespace(quote=AsyncMock(return_value=quote))
    feed = (
        None
        if feed_state == "none"
        else SimpleNamespace(
            start=AsyncMock(
                side_effect=RuntimeError("isolated feed failure")
                if feed_state == "failed"
                else None
            ),
            stop=AsyncMock(),
            on_message=Mock(),
        )
    )
    provider = LiveMarketDataProvider(
        client=SimpleNamespace(aclose=AsyncMock()),
        market_api=api,
        clock=fake_clock,
        feed=feed,
    )
    expected = HealthStatus.PASS if feed_state == "working" else HealthStatus.DEGRADED
    assert provider.status is HealthStatus.SKIPPED
    await provider.connect()
    assert provider.status is HealthStatus.SKIPPED
    for invalid in ("NaN", "Infinity", "0", "-1"):
        await provider._on_feed_message({"message": {"trading_symbol": "TEST", "ltp": invalid}})
        assert provider.status is HealthStatus.SKIPPED
    await provider.get_quote(quote.instrument)
    assert provider.status is expected
    fake_clock.advance(timedelta(seconds=16))
    assert provider.status is HealthStatus.FAIL
    api.quote.return_value = replace(quote, observed_at=fake_clock.now() + timedelta(seconds=1))
    await provider.get_quote(quote.instrument)
    assert provider.status is HealthStatus.FAIL
    api.quote.return_value = replace(quote, observed_at=fake_clock.now())
    await provider.get_quote(quote.instrument)
    assert provider.status is expected
    await provider.close()
    assert provider.status is HealthStatus.SKIPPED
