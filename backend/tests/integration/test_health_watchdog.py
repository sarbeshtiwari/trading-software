import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import HealthStatus
from app.core.errors import TradingDisabledError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode, resolve_mode
from app.monitoring.broker_checks import GrowwAuthCheck, MarketDataCheck
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthCheck, get_health_registry
from app.monitoring.watchdog import HealthWatchdog


class MutableCheck(HealthCheck):
    name = "database"
    critical = True
    status = HealthStatus.PASS

    async def run(self):
        return self.status, "fixture check", {}


def setup_watchdog(clock):
    resolve_mode(TradingMode.PAPER)
    gate = get_trading_gate()
    gate.clear("startup")
    check = MutableCheck()
    get_health_registry().register(check)
    watchdog = HealthWatchdog(0.01, required={"database"}, clock=clock)
    return watchdog, check, gate


async def events():
    async with db_session.session_scope() as session:
        return list((await session.scalars(sa.select(AuditEvent))).all())


async def test_failure_recovery_audit_and_noncritical_news(db_engine, fake_clock):
    watchdog, check, gate = setup_watchdog(fake_clock)
    await watchdog.cycle()
    assert gate.new_entries_allowed
    assert get_health_registry().last_report.degraded[0].name == "news"
    check.status = HealthStatus.FAIL
    await watchdog.cycle()
    with pytest.raises(TradingDisabledError, match="database"):
        gate.require_new_entries_allowed()
    assert gate.trading_enabled
    before = await events()
    await watchdog.cycle()
    assert len(await events()) == len(before)
    gate.block("risk", "DAILY LOSS LATCH")
    check.status = HealthStatus.PASS
    await watchdog.cycle()
    assert not gate.new_entries_allowed
    assert gate.state.reasons == ("risk: DAILY LOSS LATCH",)
    records = await events()
    assert len([record for record in records if record.event_type == "HEALTH_TRANSITION"]) == 3
    notices = [record for record in records if record.event_type == "NOTIFICATION_REQUESTED"]
    assert len(notices) == 3
    assert any(
        "NEWS SERVICE DEGRADED" in record.result["notification"]["message"] for record in notices
    )


@pytest.mark.parametrize("status", [HealthStatus.SKIPPED, HealthStatus.DEGRADED])
async def test_unknown_critical_is_not_healthy(db_engine, fake_clock, status):
    watchdog, check, gate = setup_watchdog(fake_clock)
    check.status = status
    await watchdog.cycle()
    assert not gate.new_entries_allowed
    get_health_registry().unregister("database")
    await watchdog.cycle()
    assert not gate.new_entries_allowed
    assert "database" in gate.reason()


async def test_failed_audit_prevents_recovery_and_retries(db_engine, fake_clock, monkeypatch):
    watchdog, _check, gate = setup_watchdog(fake_clock)
    persist = watchdog._persist
    monkeypatch.setattr(watchdog, "_persist", AsyncMock(side_effect=RuntimeError("private detail")))
    assert await watchdog.cycle() is None
    assert not gate.new_entries_allowed
    assert "private detail" not in gate.reason()
    assert await events() == []
    monkeypatch.setattr(watchdog, "_persist", persist)
    await watchdog.cycle()
    assert gate.new_entries_allowed
    assert len(await events()) == 2


async def test_scheduled_failure_and_graceful_shutdown(db_engine, fake_clock):
    watchdog, check, gate = setup_watchdog(fake_clock)
    await watchdog.start()
    try:
        assert gate.new_entries_allowed
        check.status = HealthStatus.FAIL

        async def wait_blocked():
            while gate.new_entries_allowed:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_blocked(), timeout=3)
        assert "database" in gate.reason()
    finally:
        await watchdog.stop()
    assert watchdog._task.done()
    assert "STOPPED" in gate.reason()


async def test_provider_failure_notices_restart_and_degraded_recovery(
    db_engine, fake_clock, settings_env
):
    settings = settings_env(
        GROWW_API_KEY="isolated-test-key", GROWW_API_SECRET="isolated-test-secret"
    )
    watchdog, _check, gate = setup_watchdog(fake_clock)
    broker = SimpleNamespace(
        name="groww", execution_realism=ExecutionRealism.REAL, ping=AsyncMock(return_value=False)
    )
    provider = SimpleNamespace(
        name="isolated-fixture", data_origin=DataOrigin.SYNTHETIC, status=HealthStatus.FAIL
    )
    registry = get_health_registry()
    registry.register(GrowwAuthCheck(broker, settings))
    registry.register(MarketDataCheck(provider, settings))
    await watchdog.cycle()
    assert not gate.new_entries_allowed
    first = len(await events())
    restored = HealthWatchdog(0.01, required={"database"}, clock=fake_clock)
    await restored.cycle()
    assert len(await events()) == first
    provider.status = HealthStatus.DEGRADED
    await restored.cycle()
    assert not gate.new_entries_allowed
    gate.block("risk", "DAILY LOSS LATCH")
    provider.status = HealthStatus.PASS
    broker.ping.return_value = True
    await restored.cycle()
    assert not gate.new_entries_allowed
    notices = [
        row.result["notification"]
        for row in await events()
        if row.event_type == "NOTIFICATION_REQUESTED"
    ]
    for kind in ("AUTH_FAILURE", "FEED_OUTAGE"):
        matching = [row for row in notices if row["event_type"] == kind]
        assert sorted(row["severity"] for row in matching) == ["CRITICAL", "INFO"]
        assert all("not live-order verification" in row["message"] for row in matching)


async def test_corrupt_persisted_health_state_cannot_rearm_after_restart(db_engine, fake_clock):
    watchdog, _check, gate = setup_watchdog(fake_clock)
    await watchdog.cycle()
    assert gate.new_entries_allowed
    async with db_engine.begin() as connection:
        await connection.execute(
            sa.update(AuditEvent)
            .where(AuditEvent.chain_id == "health:PAPER")
            .values(result={"corrupt": True})
        )
    restored = HealthWatchdog(0.01, required={"database"}, clock=fake_clock)
    assert await restored.cycle() is None
    assert not gate.new_entries_allowed
    assert "AUDIT UNAVAILABLE" in gate.reason()
