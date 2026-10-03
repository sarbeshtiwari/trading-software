"""Terminate only a disposable PostgreSQL test connection, then recover via owner API."""

import asyncio
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.db.models.trading import Order, Position, Trade
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.process_database import process_database

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(not os.environ.get("ATS_TEST_POSTGRES_URL"), reason="set ATS_TEST_POSTGRES_URL")
async def test_real_connection_loss_then_owner_recovery_keeps_entries_disabled(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, live_dashboard
):
    worker, _provider, api = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with process_database(worker.settings, "postgres", monkeypatch):
            engine = db_session.get_sessionmaker().kw["bind"]
            assert engine.pool._pre_ping
            control = create_async_engine(worker.settings.database_url, isolation_level="AUTOCOMMIT")
            captured, release = asyncio.Event(), asyncio.Event()
            target = {}

            async def suspend(connection):
                target["pid"] = await connection.fetchval("SELECT pg_backend_pid()")
                captured.set()
                await release.wait()

            def intercept(connection, cursor, statement, parameters, context, executemany):
                if not captured.is_set():
                    connection.connection.dbapi_connection.run_async(suspend)

            sa.event.listen(engine.sync_engine, "before_cursor_execute", intercept)
            operation = asyncio.create_task(worker.executor.monitor_once())
            try:
                await asyncio.wait_for(captured.wait(), timeout=10)
                database = make_url(worker.settings.database_url).database
                assert database.startswith("ats_recovery_test_")
                async with control.connect() as connection:
                    terminated = await connection.scalar(sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE pid = :pid AND datname = :database AND pid <> pg_backend_pid()"
                    ), {"pid": target["pid"], "database": database})
                    assert terminated is True
                release.set()
                with pytest.raises(SQLAlchemyError):
                    await asyncio.wait_for(operation, timeout=10)
            finally:
                release.set()
                sa.event.remove(engine.sync_engine, "before_cursor_execute", intercept)
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                await control.dispose()
            assert not worker.executor.ready and not get_trading_gate().new_entries_allowed
            async with db_session.session_scope() as session:
                entry = await session.scalar(sa.select(Order))
                assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
                assert (await session.scalar(sa.select(Position))).net_quantity == 111
            with pytest.raises(SafetyError, match="RECOVERY"):
                await worker.executor.submit(entry.proposal_id)
            monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
            if os.environ.get("ATS_TEST_BROWSER") == "1":
                process = await asyncio.create_subprocess_exec(
                    "node", str(ROOT / "frontend/tests/worker-recovery-browser.mjs"), live_dashboard,
                    env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
                    assert process.returncode == 0, stderr.decode(errors="replace")
                    assert stdout == b"WORKER_RECOVERY_ENTRIES_BLOCKED_VERIFIED\n"
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()
            response = await api.post("/api/v1/emergency", json={
                "action": "RECOVER_WORKER", "confirmation": "RECOVER PAPER WORKER",
                "reason": "Owner reviewed isolated database connection interruption",
            })
            assert response.status_code == 200, response.text
            assert response.json()["entries_blocked"]
            assert response.json()["execution"] == "WORKER_RECOVERED_ENTRIES_REMAIN_DISABLED"
            assert worker.executor.ready and worker.failed
            assert not get_trading_gate().new_entries_allowed
            async with db_session.session_scope() as session:
                assert (await session.get(SystemState, SINGLETON_ID)).new_entries_blocked
                assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
                receipt = await session.scalar(sa.select(AuditEvent).where(
                    AuditEvent.event_type == "PAPER_WORKER_RECOVERED"
                ))
                assert receipt.actor == "owner" and not receipt.result["entries_authorized"]
            await worker.executor.monitor_once()
    finally:
        await worker.stop()
        await api.aclose()
