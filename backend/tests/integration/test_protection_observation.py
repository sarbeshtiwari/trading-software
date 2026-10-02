"""Actual monitoring persists expiring evidence rather than permanent protection."""

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Position
from app.execution.paper import PaperExecution
from app.trading.worker import PaperWorker, WorkerLock
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def test_check_survives_restart_but_expires_and_detects_changes(
    db_engine, credentials, fake_clock
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        before = (await client.get("/api/v1/workspace")).json()
        assert before["positions"][0]["watchdog_observation"] == "UNAVAILABLE"
        await engine.monitor_once()
        checked = (await client.get("/api/v1/workspace")).json()
        assert checked["positions"][0]["watchdog_observation"] == "RECENT_SOFTWARE_CHECK"
        assert not checked["positions"][0]["is_protected"]
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restarted.recover()
        await restarted.verify_protection()
        assert (await client.get("/api/v1/workspace")).json()["positions"][0][
            "watchdog_observation"
        ] == "RECENT_SOFTWARE_CHECK"
        fake_clock.advance_seconds(16)
        assert (await client.get("/api/v1/workspace")).json()["positions"][0][
            "watchdog_observation"
        ] == "STALE"
        with pytest.raises(SafetyError, match="STALE_OR_INVALID_QUOTE"):
            await restarted.monitor_once()
        market["observed"] = fake_clock.now()
        await restarted.monitor_once()
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            position.stop_loss_price = None
        assert (await client.get("/api/v1/workspace")).json()["positions"][0][
            "watchdog_observation"
        ] == "CHANGED_SINCE_CHECK"
        with pytest.raises(SafetyError, match="PROTECTION_MISSING_OR_CHANGED"):
            await restarted.verify_protection()
        state = (await client.get("/api/v1/workspace")).json()
        assert state["positions"][0]["net_quantity"] == 0
        assert state["positions"][0]["watchdog_observation"] == "FLAT"
    finally:
        await client.aclose()


async def test_worker_restart_refuses_missing_stop_and_releases_lock(
    db_engine, credentials, fake_clock, tmp_path
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            position.stop_loss_price = None
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        worker = PaperWorker(restarted, lock_path=tmp_path / "watchdog.lock")
        with pytest.raises(SafetyError, match="PROTECTION_MISSING_OR_CHANGED"):
            await worker.start(schedule=False)
        assert not worker.running
        contender = WorkerLock(tmp_path / "watchdog.lock")
        contender.acquire()
        contender.release()
        async with db_session.session_scope() as session:
            assert (await session.scalar(sa.select(Position))).net_quantity == 0
    finally:
        await client.aclose()


async def test_altered_watchdog_audit_is_not_reported_as_recent(db_engine, credentials, fake_clock):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await engine.monitor_once()
        async with db_session.session_scope() as session:
            record = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "PROTECTION_CHECKED")
            )
            record.result = {**record.result, "quantity": 999}
        state = (await client.get("/api/v1/workspace")).json()
        assert state["positions"][0]["watchdog_observation"] == "INTEGRITY_FAILURE"
    finally:
        await client.aclose()
