"""Audited health observations, interval boundaries and authenticated inspection."""

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import health
from app.core.enums import HealthStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.main import create_app
from app.modes import TradingMode
from app.monitoring import history
from app.security.auth import OwnerAuth
from tests.integration.test_auth import credentials
from tests.integration.test_health_watchdog import setup_watchdog
from tests.integration.test_paper_browser import ROOT, live_dashboard

__all__ = ["credentials", "live_dashboard"]
POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")


async def observations(fake_clock):
    watchdog, check, _gate = setup_watchdog(fake_clock)
    await watchdog.cycle()
    start = fake_clock.utcnow()
    fake_clock.advance(timedelta(hours=1))
    check.status = HealthStatus.FAIL
    await watchdog.cycle()
    fake_clock.advance(timedelta(hours=1))
    check.status = HealthStatus.PASS
    await watchdog.cycle()
    return start


async def test_health_history_bounds_paging_and_browser(
    db_engine, credentials, fake_clock, live_dashboard
):
    start = await observations(fake_clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as api:
        assert (await api.get("/api/v1/health/history")).status_code == 401
        token, _refresh = await OwnerAuth().login("owner", credentials[1])
        api.headers["Authorization"] = f"Bearer {token}"
        params = {"from_at": (start + timedelta(hours=1)).isoformat(),
                  "to_at": fake_clock.utcnow().isoformat(), "limit": 1}
        first = await api.get("/api/v1/health/history", params=params)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["preceding"]["state"]["checks"]["database"]["status"] == "PASS"
        assert result["observations"][0]["state"]["checks"]["database"]["status"] == "FAIL"
        assert result["has_more"] and result["next_offset"] == 1
        second = (await api.get("/api/v1/health/history", params={**params, "offset": 1})).json()
        assert second["observations"][0]["state"]["checks"]["database"]["status"] == "PASS"
        assert not second["has_more"]
        assert second["observations"][0]["audit_id"] != result["observations"][0]["audit_id"]
        empty = (await api.get("/api/v1/health/history", params={
            "from_at": (start + timedelta(minutes=1)).isoformat(),
            "to_at": (start + timedelta(minutes=59)).isoformat(),
        })).json()
        assert empty["observations"] == [] and empty["preceding"] is not None
        for invalid in [
            {"from_at": "2026-01-01T00:00:00"},
            {"from_at": (start - timedelta(days=32)).isoformat()},
            {"to_at": (fake_clock.utcnow() + timedelta(seconds=1)).isoformat()},
            {"from_at": fake_clock.utcnow().isoformat(), "to_at": start.isoformat()},
            {"limit": 101}, {"offset": -1},
        ]:
            assert (await api.get("/api/v1/health/history", params=invalid)).status_code == 422
        if os.environ.get("ATS_TEST_BROWSER") == "1":
            process = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "frontend/tests/health-history-browser.mjs"), live_dashboard,
                env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                assert process.returncode == 0, stderr.decode(errors="replace")
                assert stdout == b"HEALTH_HISTORY_BROWSER_VERIFIED\n"
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()


async def test_history_integrity_capacity_and_storage_fail_closed(
    db_engine, credentials, fake_clock, monkeypatch
):
    await observations(fake_clock)
    token, _refresh = await OwnerAuth().login("owner", credentials[1])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as api:
        with monkeypatch.context() as context:
            context.setattr(history, "MAX_RECORDS", 1)
            assert (await api.get("/api/v1/health/history")).status_code == 413
        with monkeypatch.context() as context:
            context.setattr(health, "read_history", AsyncMock(side_effect=RuntimeError("private database failure")))
            unavailable = await api.get("/api/v1/health/history")
            assert unavailable.status_code == 503 and "private" not in unavailable.text
        async with db_session.session_scope() as session:
            await session.execute(sa.update(AuditEvent).where(AuditEvent.chain_id == "health:PAPER")
                                  .values(actor="tampered"))
        damaged = await api.get("/api/v1/health/history")
        assert damaged.status_code == 409 and "tampered" not in damaged.text


@pytest.mark.skipif(not POSTGRES_URL, reason="set ATS_TEST_POSTGRES_URL")
async def test_postgres_persisted_health_history(fake_clock, monkeypatch):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(sa.text(
                    "CREATE TEMP TABLE audit_events (LIKE public.audit_events INCLUDING ALL) ON COMMIT DROP"
                ))
                await connection.execute(sa.text(
                    "CREATE TEMP TABLE runtime_event_outbox (LIKE public.runtime_event_outbox INCLUDING ALL) ON COMMIT DROP"
                ))
                monkeypatch.setattr(db_session, "_sessionmaker", async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                ))
                start = await observations(fake_clock)
                async with db_session.session_scope() as session:
                    result = await history.read_history(
                        session, TradingMode.PAPER, start, fake_clock.utcnow(), fake_clock.utcnow()
                    )
                assert [row.state.checks["database"].status for row in result.observations] == [
                    HealthStatus.PASS, HealthStatus.FAIL, HealthStatus.PASS,
                ]
                assert result.preceding is None
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
