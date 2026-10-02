"""Actual worker quote ingestion recovers health without bypassing entry gates."""

from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.core.enums import HealthStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order
from app.modes import TradingMode, resolve_mode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthRegistry
from app.monitoring.watchdog import HealthWatchdog
from app.risk.safety import RiskSafety
from app.trading import worker as worker_module
from app.trading.health import PaperMarketDataCheck, PaperMarketTransportCheck, PaperRuntimeCheck
from tests.integration.test_auth import credentials
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


@pytest.mark.parametrize("latched", [False, True])
async def test_selected_provider_bootstraps_while_entries_blocked(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, latched
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    monkeypatch.setattr(worker_module._runtime, "worker", worker)
    worker.settings.paper_worker_enabled = True
    provider.status = HealthStatus.DEGRADED
    resolve_mode(TradingMode.PAPER)
    registry = HealthRegistry()
    registry.register(PaperMarketDataCheck())
    registry.register(PaperMarketTransportCheck())
    registry.register(PaperRuntimeCheck(worker.settings, clock=fake_clock))
    watchdog = HealthWatchdog(
        30, registry=registry, required={"market_data", "order_service"}, clock=fake_clock
    )
    try:
        if latched:
            await RiskSafety().trip_error("PAPER", "SYNTHETIC")
        await watchdog.cycle()
        assert not get_trading_gate().state.new_entries_allowed
        await worker.cycle()
        assert not worker.failed, worker.detail
        provider.get_quote.assert_awaited()
        provider.get_candles.assert_not_awaited()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        report = await watchdog.cycle()
        checks = {result.name: result for result in report.results}
        assert checks["market_data"].status == HealthStatus.PASS
        assert checks["market_transport"].status == HealthStatus.DEGRADED
        assert not checks["market_transport"].critical
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == (
                0 if latched else 1
            )
        if latched:
            provider.get_candles.assert_not_awaited()
            assert not get_trading_gate().state.new_entries_allowed
        else:
            response = await client.get("/api/v1/workspace")
            assert response.status_code == 200
            assert len(response.json()["orders"]) == 1
    finally:
        await worker.stop()
        await client.aclose()


async def test_warmup_is_audited_stand_down_not_worker_failure(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    provider.get_candles.return_value = provider.get_candles.return_value[-10:]
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            events = list((await session.scalars(sa.select(AuditEvent))).all())
            assert any(
                event.event_type == "REFERENCE_STAND_DOWN"
                and "REFERENCE_WARMUP_UNAVAILABLE" in str(event.result)
                for event in events
            )
    finally:
        await worker.stop()
        await client.aclose()


async def test_stale_refresh_cannot_reuse_previously_healthy_observation(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    monkeypatch.setattr(worker_module._runtime, "worker", worker)
    get_trading_gate().block("test_read_only", "Only read-only refresh permitted")
    try:
        await worker.cycle()
        assert (await PaperMarketDataCheck().run())[0] == HealthStatus.PASS
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now() - timedelta(hours=1)
        )
        await worker.cycle()
        assert worker.failed
        assert (await PaperMarketDataCheck().run())[0] == HealthStatus.FAIL
        assert not worker.reference_runtime.latest_quotes
        provider.get_candles.assert_not_awaited()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
    finally:
        await worker.stop()
        await client.aclose()
