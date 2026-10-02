"""Separate CLI process shares the worker database, without an HTTP server."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.calendar import TradingCalendar
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.security import DashboardSession
from app.emergency.controls import EmergencyControls
from app.trading.worker import PaperWorker
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def invoke(password, confirmation="KILL PAPER", environment=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.emergency.cli",
        "kill",
        "--reason",
        "Owner tests standalone emergency inhibition",
        "--confirm",
        confirmation,
        "--password-stdin",
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            **(environment or {}),
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate((password + "\n").encode()), timeout=30
        )
        return process.returncode, stdout.decode(), stderr.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_standalone_cli_persists_kill_and_executor_honours_it(
    db_engine, credentials, fake_clock, tmp_path
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "cli-worker.lock",
    )
    try:
        await worker.start(schedule=False)
        code, output, errors = await invoke(credentials[1])
        assert code == 0, errors
        assert json.loads(output)["execution"] == "ENTRY_INHIBITION_ONLY"
        assert credentials[1] not in output + errors
        assert (await EmergencyControls().restore())["kill_switch"]
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert await engine.broker.list_orders() == []
        await worker.stop()
        await worker.start(schedule=False)
        await worker.cycle()
        assert (await EmergencyControls().restore())["kill_switch"]
        with pytest.raises(SafetyError, match="EMERGENCY"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        assert await AuditService().verify("paper-emergency", expected_count=1)
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "EMERGENCY_ACTIVATED")
            )
            assert event.actor == "owner"
            logout = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "LOGOUT")
            )
            assert (await session.get(DashboardSession, logout.result["session_id"])).revoked
    finally:
        await worker.stop()
        await client.aclose()


async def test_cli_storage_failure_never_prints_success(db_engine, credentials, tmp_path):
    code, output, errors = await invoke(
        credentials[1],
        environment={
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'no-schema.db'}",
        },
    )
    assert code == 1
    assert "ENTRY_INHIBITION_ONLY" not in output
    assert "not confirmed" in errors
    assert credentials[1] not in output + errors
    assert not (await EmergencyControls().restore())["kill_switch"]


async def test_standalone_cli_refuses_bad_password_and_confirmation(
    db_engine, credentials, fake_clock
):
    _, _, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        code, output, errors = await invoke("incorrect-isolated-password")
        assert code == 1
        assert "incorrect-isolated-password" not in output + errors
        code, output, errors = await invoke(credentials[1], "wrong confirmation")
        assert code == 1
        assert "CONFIRMATION_MISMATCH" in errors
        assert not (await EmergencyControls().restore())["kill_switch"]
        async with db_session.session_scope() as session:
            events = set(await session.scalars(sa.select(AuditEvent.event_type)))
        assert {"LOGIN_DENIED", "EMERGENCY_REQUEST_REFUSED", "LOGOUT"} <= events
    finally:
        await client.aclose()
