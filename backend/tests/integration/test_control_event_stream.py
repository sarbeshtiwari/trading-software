"""Real durable risk/health transitions reach read-only operational consumers."""

import asyncio
import json
import os

import pytest
import sqlalchemy as sa

from app.api import workspace_stream
from app.core.enums import HealthStatus
from app.core.events import RedisStreamEventBus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.monitoring import watchdog as watchdog_module
from app.monitoring.gate import get_trading_gate
from app.monitoring.runtime_events import RuntimeEventPublisher
from app.risk import safety as safety_module
from app.risk.safety import RiskSafety
from tests.integration.test_auth import credentials
from tests.integration.test_health_watchdog import setup_watchdog
from tests.integration.test_paper_browser import (
    HUNG_BROWSER_SECONDS,
    ROOT,
    browser_output,
    live_dashboard,
)
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_redis_events import URL, redis_events

__all__ = ["credentials", "live_dashboard", "redis_events"]


@pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")
async def test_risk_health_transitions_reach_stream_and_current_api(
    db_engine, credentials, fake_clock, redis_events, live_dashboard, monkeypatch
):
    client, prefix = redis_events
    credentials[0].redis_url = URL
    monkeypatch.setattr(workspace_stream, "STREAMS", (
        f"{prefix}:system.risk_state_changed", f"{prefix}:system.health_changed",
    ))
    engine, _proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    publisher = RuntimeEventPublisher(
        engine.settings, bus=RedisStreamEventBus(client, stream_prefix=prefix), clock=fake_clock
    )
    process = None
    try:
        if os.environ.get("ATS_TEST_BROWSER") == "1":
            process = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "frontend/tests/risk-stream-browser.mjs"), live_dashboard,
                env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            assert await asyncio.wait_for(
                process.stdout.readline(), HUNG_BROWSER_SECONDS
            ) == b"RISK_STREAM_READY\n"
        await RiskSafety(clock=fake_clock).trip_error("PAPER", "SYNTHETIC")
        await RiskSafety(clock=fake_clock).trip_error("PAPER", "SYNTHETIC")
        assert await publisher.publish_once() == 1
        assert (await api.get("/api/v1/risk")).json()["latches"]["SYNTHETIC"]["engine_error"]
        assert not (await api.get("/api/v1/workspace")).json()["new_entries_allowed"]
        if process is not None:
            stdout, stderr = await browser_output(process)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert stdout == b"RISK_STREAM_BROWSER_VERIFIED\n"
        watchdog, check, gate = setup_watchdog(fake_clock)
        await watchdog.cycle()
        check.status = HealthStatus.FAIL
        await watchdog.cycle()
        await watchdog.cycle()
        assert await publisher.publish_once() == 2
        assert not gate.new_entries_allowed
        assert "database" in gate.reason()
        events = [json.loads(fields[b"data"]) for _identifier, fields in
                  await client.xrange(f"{prefix}:system.health_changed")]
        assert len(events) == 2 and all(event["source"] == "health_watchdog" for event in events)
        assert all(event["payload"]["execution_realism"] is None for event in events)
        assert await RuntimeEventPublisher(engine.settings, bus=publisher.bus).publish_once() == 0
        assert await engine.broker.list_orders() == []
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await api.aclose()


@pytest.mark.parametrize("source", ["risk", "health"])
async def test_intent_failure_rolls_back_control_evidence_and_blocks(
    db_engine, credentials, fake_clock, monkeypatch, source
):
    def fail(**kwargs):
        raise RuntimeError("fixture intent failure")

    if source == "risk":
        monkeypatch.setattr(safety_module, "RuntimeEventOutbox", fail)
        with pytest.raises(RuntimeError, match="intent failure"):
            await RiskSafety(clock=fake_clock).trip_error("PAPER", "SYNTHETIC")
    else:
        monkeypatch.setattr(watchdog_module, "RuntimeEventOutbox", fail)
        watchdog, _check, _gate = setup_watchdog(fake_clock)
        assert await watchdog.cycle() is None
    assert not get_trading_gate().new_entries_allowed
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 0
