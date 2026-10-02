"""Cross-database report publication never overwrites the destination PAPER account."""

import asyncio
import json
import os
import sys
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService
from app.audit.snapshots import canonical
from app.backtest.bootstrap import prepare_run
from app.backtest.catalog import column_values
from app.backtest.engine import run_prepared
from app.backtest.publication import publish_run
from app.config import get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.base import Base
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun
from app.monitoring.gate import get_trading_gate, reset_trading_gate
from tests.conftest import BACKEND_ROOT
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials", "isolated_database"]


async def prepare_source(fake_clock, tmp_path):
    recording, request = recorded_trade()
    fake_clock.set_to(request.start_at)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "source.lock",
    )
    assert await run_prepared(worker, request) == "COMPLETED"
    return request


@pytest.fixture
async def catalog_database(isolated_database, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


def select_target(monkeypatch, engine):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
    )


async def account_snapshot():
    excluded = {"backtest_runs", "backtest_results", "backtest_trades", "audit_events"}
    async with db_session.session_scope() as session:
        values = {}
        for table in Base.metadata.sorted_tables:
            if table.name not in excluded:
                rows = (
                    (await session.execute(sa.select(table).order_by(*table.primary_key.columns)))
                    .mappings()
                    .all()
                )
                values[table.name] = [column_values(row) for row in rows]
        return canonical(values)


async def test_publication_is_idempotent_and_preserves_active_account(
    isolated_database, catalog_database, credentials, fake_clock, tmp_path, monkeypatch
):
    request = await prepare_source(fake_clock, tmp_path)
    select_target(monkeypatch, catalog_database)
    reset_trading_gate()
    worker, _, client = await setup_worker(
        credentials, fake_clock, tmp_path / "catalog", costed=True
    )
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        before = await account_snapshot()
        gate = get_trading_gate().state.reasons
        source_settings = tmp_path / "source-settings.json"
        source_settings.write_text(
            json.dumps({"database_url": str(isolated_database.url)}), encoding="utf-8"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.backtest.publish",
            str(source_settings),
            request.run_id,
            request.recording_sha256,
            cwd=tmp_path,
            env={
                **os.environ,
                "PYTHONPATH": str(BACKEND_ROOT),
                "DATABASE_URL": str(catalog_database.url),
                "TRADING_MODE": "PAPER",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert json.loads(stdout)["status"] == "PUBLISHED"
            assert json.loads(stdout)["simulated"] is True
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        assert (
            await publish_run(
                isolated_database,
                request.run_id,
                request.recording_sha256,
                actor="fixture-owner",
                clock=fake_clock,
            )
            == "ALREADY_PUBLISHED"
        )
        assert await account_snapshot() == before
        assert get_clock() is fake_clock and get_trading_gate().state.reasons == gate
        assert await AuditService(fake_clock).verify(f"history:{request.run_id}")
        assert await AuditService(fake_clock).verify(f"publication:{request.run_id}")
        response = await client.get(f"/api/v1/backtests/{request.run_id}")
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["net_pnl"] == "856.56"
        assert (
            len((await client.get(f"/api/v1/backtests/{request.run_id}/samples")).json()["samples"])
            == 3
        )
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("problem", ["result", "audit", "recording", "running"])
async def test_invalid_sources_never_partially_publish(
    isolated_database, catalog_database, fake_clock, tmp_path, monkeypatch, problem
):
    request = await prepare_source(fake_clock, tmp_path)
    async with db_session.session_scope() as session:
        if problem == "result":
            (await session.scalar(sa.select(BacktestResult))).net_pnl = Decimal(999999)
        elif problem == "audit":
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "HISTORICAL_ACCOUNT_SAMPLE")
            )
            event.result = {"modified": True}
        elif problem == "running":
            (await session.get(BacktestRun, request.run_id)).status = "RUNNING"
    select_target(monkeypatch, catalog_database)
    with pytest.raises(ValueError):
        await publish_run(
            isolated_database,
            request.run_id,
            "0" * 64 if problem == "recording" else request.recording_sha256,
            actor="fixture-owner",
            clock=fake_clock,
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestRun)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 0


async def test_publication_audit_failure_rolls_back_entire_catalog(
    isolated_database, catalog_database, fake_clock, tmp_path, monkeypatch
):
    request = await prepare_source(fake_clock, tmp_path)
    select_target(monkeypatch, catalog_database)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditService, "append_in_session", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await publish_run(
            isolated_database,
            request.run_id,
            request.recording_sha256,
            actor="fixture-owner",
            clock=fake_clock,
        )
    async with db_session.session_scope() as session:
        for model in (BacktestRun, BacktestResult, AuditEvent):
            assert await session.scalar(sa.select(sa.func.count()).select_from(model)) == 0
