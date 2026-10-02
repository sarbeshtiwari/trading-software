from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.core.enums import HealthStatus, OrderStatus
from app.db import session as db_session
from app.db.models.trading import Order, Position
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.monitoring.watchdog import HealthWatchdog
from app.trading.health import PaperRuntimeCheck
from app.trading.worker import _runtime
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials"]


@pytest.mark.parametrize("fault", ["stale", "unknown", "protection"])
async def test_runtime_health_uses_actual_worker_and_blocks_without_placing_orders(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, fault
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    monkeypatch.setattr(_runtime, "worker", worker)
    settings = worker.settings.model_copy(update={"paper_worker_enabled": True})
    check = PaperRuntimeCheck(settings, clock=fake_clock)
    try:
        await worker.cycle()
        await worker.cycle()
        async with worker.cycle_lock:
            result = await check.execute()
        assert result.status is HealthStatus.PASS, result.detail
        assert result.context["open_positions"] == 1
        assert result.context["execution_realism"] == "SIMULATED"
        get_health_registry().register(check)
        watchdog = HealthWatchdog(60, required={"order_service"}, clock=fake_clock)
        if fault == "stale":
            fake_clock.advance(timedelta(seconds=settings.paper_cycle_seconds * 3 + 1))
        else:
            async with db_session.session_scope() as session:
                if fault == "unknown":
                    order = await session.scalar(sa.select(Order))
                    order.status = OrderStatus.UNKNOWN
                else:
                    position = await session.scalar(sa.select(Position))
                    position.stop_loss_price = None
        await watchdog.cycle()
        assert "health_watchdog" in get_trading_gate().reason()
        assert get_health_registry().last_report.failing_critical[0].name == "order_service"
        assert len(await worker.executor.broker.list_orders()) == 1
    finally:
        await worker.stop()
        await client.aclose()


async def test_disabled_and_missing_worker_are_distinct(settings_env, monkeypatch):
    monkeypatch.setattr(_runtime, "worker", None)
    disabled = PaperRuntimeCheck(settings_env(PAPER_WORKER_ENABLED=False))
    assert (await disabled.execute()).status is HealthStatus.SKIPPED
    assert not disabled.critical
    missing = PaperRuntimeCheck(settings_env(PAPER_WORKER_ENABLED=True))
    assert (await missing.execute()).status is HealthStatus.FAIL
    assert missing.critical
