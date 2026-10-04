"""Runtime wiring with isolated recording channels, never remote delivery claims."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import Response

from app.agents.pipeline import DecisionPipeline
from app.api.health import ready
from app.api.workspace import workspace
from app.audit.service import AuditService
from app.config import Settings
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.main import create_app
from app.monitoring.gate import get_trading_gate, reset_trading_gate
from app.monitoring.healthchecks import HealthReport
from app.monitoring.startup import run_startup_checks
from app.notifications import runtime
from app.notifications.outbox import NotificationOutbox
from app.risk.safety import RiskSafety
from tests.integration.test_notification_outbox import requests
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_notifications import RecordingChannel
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_risk import limits, market, portfolio


@pytest_asyncio.fixture(autouse=True)
async def isolated_runtime(fake_clock):
    fake_clock.set_to(OBSERVED)
    await runtime.stop_notifications()
    yield
    await runtime.stop_notifications()


def configured(**changes):
    return Settings(notification_enabled=True, notification_retry_delay_seconds=0, **changes)


async def test_disabled_missing_and_malformed_configuration(fake_clock):
    assert await runtime.start_notifications(Settings()) is None
    assert (
        runtime.emit_critical(
            event_id="test",
            event_type="TEST",
            message="test",
            condition_key="test",
            occurred_at=fake_clock.now(),
        )
        == "DISABLED"
    )
    service = await runtime.start_notifications(
        configured(notification_quiet_start="", notification_quiet_end="")
    )
    assert not service.channels
    assert (
        runtime.emit_critical(
            event_id="test",
            event_type="TEST",
            message="test",
            condition_key="test",
            occurred_at=fake_clock.now(),
        )
        == "QUEUED"
    )
    await service.drain()
    assert {row[2] for row in service.outcomes} == {"DISABLED"}
    assert await runtime.start_notifications(configured(notification_quiet_start="22:00")) is None
    assert runtime.current_service() is None


async def test_monitoring_never_calls_stopped_delivery_tasks_running(db_engine, monkeypatch):
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {
        "email": RecordingChannel()
    })
    service = await runtime.start_notifications(configured())
    assert runtime.status()["status"] == "STARTING"
    for _attempt in range(100):
        if runtime.status()["status"] == "RUNNING":
            break
        await asyncio.sleep(0.01)
    assert runtime.status()["status"] == "RUNNING"
    await service.stop()
    state = await workspace()
    notice = next(item for item in state.components if item.name == "notifications")
    assert notice.status == "DEGRADED" and "not running" in notice.detail
    await service.start()
    runtime._runtime.outbox_task.cancel()
    await asyncio.gather(runtime._runtime.outbox_task, return_exceptions=True)
    state = await workspace()
    notice = next(item for item in state.components if item.name == "notifications")
    assert notice.status == "DEGRADED" and "not running" in notice.detail


async def test_monitoring_reports_outbox_storage_failure_and_recovery(db_engine, monkeypatch):
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {
        "email": RecordingChannel()
    })
    original = NotificationOutbox.dispatch_once
    failing = True

    async def dispatch(outbox):
        if failing:
            raise sa.exc.OperationalError("isolated storage failure", None, None)
        return await original(outbox)

    monkeypatch.setattr(NotificationOutbox, "dispatch_once", dispatch)
    await runtime.start_notifications(configured())
    for _attempt in range(100):
        if runtime.status()["status"] == "DEGRADED":
            break
        await asyncio.sleep(0.01)
    state = await workspace()
    notice = next(item for item in state.components if item.name == "notifications")
    assert notice.status == "DEGRADED" and "outbox unavailable" in notice.detail
    failing = False
    for _attempt in range(600):
        if runtime.status()["status"] == "RUNNING":
            break
        await asyncio.sleep(0.01)
    assert runtime.status()["status"] == "RUNNING"


async def test_risk_breach_notifies_only_after_durable_audit(db_engine, monkeypatch):
    class AuditedChannel(RecordingChannel):
        async def send(self, notification):
            async with db_session.session_scope() as session:
                request = await session.scalar(
                    sa.select(AuditEvent).where(
                        AuditEvent.chain_id == notification.event_id, AuditEvent.sequence == 1
                    )
                )
                event = await session.get(AuditEvent, request.result["source_audit_id"])
                assert event is not None and event.severity.value == "CRITICAL"
            await super().send(notification)

    channel = AuditedChannel()
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {"email": channel})
    service = await runtime.start_notifications(configured())
    context = await setup_context()
    context = context.model_copy(
        update={
            "portfolio": portfolio(strategy_id="test-only", realised_day_pnl=-2000, equity=90000)
        }
    )
    result = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert result.approved_quantity == 0
    await NotificationOutbox(service).dispatch_once()
    await service.drain()
    assert {item.event_type for item in channel.messages} == {"DAILY_LOSS_LIMIT", "DRAWDOWN_LIMIT"}
    assert all(item.severity.value == "CRITICAL" for item in channel.messages)
    await quant_decision(DecisionPipeline(validator()), payload(), context)
    await NotificationOutbox(service).dispatch_once()
    await service.drain()
    assert len(channel.messages) == 2


async def test_engine_failure_notifies_and_delivery_failure_cannot_enable_entries(
    db_engine, monkeypatch
):
    channel = RecordingChannel(failures=20)
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {"email": channel})
    service = await runtime.start_notifications(configured())
    context = await setup_context()

    def broken(*args):
        raise RuntimeError("test-only risk exception")

    monkeypatch.setattr("app.risk.engine.RULES", (broken,))
    pipeline = DecisionPipeline(validator())
    assert (await quant_decision(pipeline, payload(), context)).code == "RISK_ENGINE_ERROR"
    outbox = NotificationOutbox(service)
    for _attempt in range(3):
        await outbox.dispatch_once()
    await service.drain()
    assert channel.calls == 3
    assert not pipeline.safety.gate.new_entries_allowed
    assert (await quant_decision(pipeline, payload(), context)).code == "RISK_ERROR_LATCHED"


async def test_disabled_delivery_preserves_risk_notice_across_restart(db_engine):
    safety = RiskSafety()
    await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    notices = await requests()
    assert len(notices) == 1
    assert notices[0].result["notification"]["event_type"] == "DAILY_LOSS_LIMIT"
    await db_session.dispose_engine()
    db_session.init_engine()
    restored = RiskSafety()
    assert (await restored.restore("PAPER", "SYNTHETIC")).daily_loss
    await restored.observe(portfolio(), market(), limits())
    assert [row.id for row in await requests()] == [notices[0].id]
    assert not restored.gate.new_entries_allowed


async def test_new_session_breach_notifies_even_if_previous_day_was_latched(db_engine, fake_clock):
    safety = RiskSafety()
    await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    tomorrow = OBSERVED + timedelta(days=1)
    fake_clock.set_to(tomorrow)
    state = portfolio(realised_day_pnl=-2000, observed_at=tomorrow, available_at=tomorrow)
    prices = market(as_of=tomorrow, observed_at=tomorrow, available_at=tomorrow)
    await safety.observe(state, prices, limits())
    await safety.observe(state, prices, limits())
    assert len(await requests()) == 2
    records = await AuditService().chain(safety.chain_id("PAPER", "SYNTHETIC"))
    assert [row.severity.value for row in records] == ["CRITICAL", "CRITICAL", "INFO"]


async def test_risk_notice_storage_failure_is_atomic_and_fail_closed(db_engine, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("isolated durable notice failure")

    monkeypatch.setattr("app.risk.safety.enqueue", unavailable)
    safety = RiskSafety()
    with pytest.raises(RuntimeError):
        await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    assert not safety.gate.new_entries_allowed
    assert "RISK_SAFETY_UNAVAILABLE" in safety.gate.reason()
    assert await requests() == []
    assert await AuditService().chain(safety.chain_id("PAPER", "SYNTHETIC")) == []


async def test_storage_failure_reports_degradation_not_committed_breach(db_engine, monkeypatch):
    channel = RecordingChannel()
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {"email": channel})
    service = await runtime.start_notifications(configured())

    async def unavailable(*args, **kwargs):
        raise RuntimeError("test-only storage unavailable")

    monkeypatch.setattr(AuditService, "append_in_session", unavailable)
    with pytest.raises(RuntimeError):
        await RiskSafety().trip_error("PAPER", "SYNTHETIC")
    await service.drain()
    assert [item.event_type for item in channel.messages] == ["RISK_SAFETY_UNAVAILABLE"]


async def test_notification_submission_error_is_isolated(fake_clock, monkeypatch):
    service = await runtime.start_notifications(configured())

    def broken(*args):
        raise RuntimeError("test-only submission failure")

    monkeypatch.setattr(service, "submit", broken)
    assert (
        runtime.emit_critical(
            event_id="test",
            event_type="TEST",
            message="test",
            condition_key="test",
            occurred_at=fake_clock.now(),
        )
        == "FAILED"
    )


async def test_real_lifespan_starts_and_stops_configured_worker(db_engine, monkeypatch):
    settings = configured()
    channel = RecordingChannel()
    monkeypatch.setattr(runtime, "configured_channels", lambda *args, **kwargs: {"email": channel})
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def isolated_checks(*args):
        return SimpleNamespace(trading_allowed=False, blocking_reason="test-only startup block")

    monkeypatch.setattr("app.main.run_startup_checks", isolated_checks)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.notifications is runtime.current_service()
        assert runtime.current_service() is not None
    assert runtime.current_service() is None


async def test_lifespan_restores_entry_latch_before_startup_checks(db_engine, monkeypatch):
    await RiskSafety().trip_error("PAPER", "SYNTHETIC")
    reset_trading_gate()
    settings = Settings()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def checks(*args):
        gate = get_trading_gate()
        gate.clear("startup")
        assert not gate.new_entries_allowed
        assert gate.trading_enabled
        assert "RISK_ERROR_LATCHED" in gate.reason()
        return SimpleNamespace(trading_allowed=False, blocking_reason=gate.reason())

    monkeypatch.setattr("app.main.run_startup_checks", checks)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert not get_trading_gate().new_entries_allowed
        assert get_trading_gate().trading_enabled
        response = Response()
        report = await ready(response)
        assert response.status_code == 503 and report["trading_allowed"] is False
        assert report["status"] == "FAIL"
        assert "RISK_ERROR_LATCHED" in report["blocking_reason"]


async def test_startup_result_does_not_hide_existing_entry_block(fake_clock, monkeypatch):
    async def healthy_dependencies():
        return HealthReport(results=(), generated_at=fake_clock.now())

    registry = SimpleNamespace(names=("test-only",), run_all=healthy_dependencies)
    monkeypatch.setattr("app.monitoring.startup.get_health_registry", lambda: registry)
    get_trading_gate().block("test-risk-latch", "DRAWDOWN_LATCHED")
    result = await run_startup_checks(Settings())
    assert not result.trading_allowed
    assert result.to_dict()["trading_allowed"] is False
    assert "DRAWDOWN_LATCHED" in result.to_dict()["blocking_reason"]
    assert get_trading_gate().trading_enabled
