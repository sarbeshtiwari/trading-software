"""Actual isolated runner, durable controller and report publication."""

import asyncio
import json

import httpx
import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.backtest.jobs import HistoricalJobs
from app.backtest.plans import load_registry, resolve_input
from app.config import get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.backtest import BacktestResult
from app.db.models.historical_jobs import HistoricalJob
from app.main import create_app
from app.marketdata.recordings import write_recording
from tests.integration.test_auth import credentials
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade
from tests.integration.test_historical_publication import catalog_database, select_target

__all__ = ["catalog_database", "credentials", "isolated_database"]


def plan_files(tmp_path, source, *, recorded=None):
    recording, manifest = recorded or recorded_trade()
    (tmp_path / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    write_recording(tmp_path / "recording.json", recording)
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "database_url": str(source.url),
                "starting_capital": "100000",
            }
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "plans.json"
    registry.write_text(
        json.dumps(
            {
                "plans": [
                    {
                        "id": manifest.run_id,
                        "label": "Isolated deterministic fixture",
                        "manifest": "manifest.json",
                        "recording": "recording.json",
                        "settings": "settings.json",
                        "timeout_seconds": 90,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return registry


async def test_job_runs_real_child_and_publishes_once(
    isolated_database,
    catalog_database,
    tmp_path,
    monkeypatch,
    fake_clock,
):
    registry = plan_files(tmp_path, isolated_database)
    select_target(monkeypatch, catalog_database)
    manager = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    original_clock = get_clock()
    try:
        identifier = await manager.enqueue(
            "fixture01", actor="owner", reason="Explicit fixture research"
        )
        task = manager.tasks[identifier]
        assert (
            await manager.enqueue(identifier, actor="owner", reason="Duplicate request")
            == identifier
        )
        assert manager.tasks[identifier] is task
        await asyncio.wait_for(asyncio.shield(task), 100)
        async with db_session.session_scope() as session:
            job = await session.get(HistoricalJob, identifier)
            assert (job.status, job.published, job.slot) == ("COMPLETED", True, None), (
                job.error_code
            )
            assert job.progress_pct == 100
            result = (await session.execute(sa.select(BacktestResult))).scalar_one()
            assert str(result.net_pnl) == "856.56"
        assert get_clock() is original_clock
        assert await AuditService(fake_clock).verify("hjob:fixture01")
        restarted = HistoricalJobs(manager.settings)
        assert (
            await restarted.enqueue(identifier, actor="owner", reason="Restart duplicate")
            == identifier
        )
        assert not restarted.tasks
    finally:
        await manager.stop()


def test_plan_paths_and_registry_fail_closed(tmp_path):
    root = tmp_path / "owner"
    root.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    for name in (str(outside), "../secret.json"):
        with pytest.raises(ValueError):
            resolve_input(root, name)
    assert load_registry(None) == ()
    registry = root / "plans.json"
    plan = {
        "id": "fixture01",
        "label": "fixture",
        "manifest": "a",
        "recording": "b",
        "settings": "c",
    }
    registry.write_text(json.dumps({"plans": [plan, plan]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_registry(registry)


async def test_authenticated_api_refuses_paths_and_preserves_orphan_reservation(
    isolated_database,
    catalog_database,
    credentials,
    tmp_path,
    monkeypatch,
    fake_clock,
):
    registry = plan_files(tmp_path, isolated_database)
    select_target(monkeypatch, catalog_database)
    settings, password = credentials
    app = create_app(settings.model_copy(update={"historical_plans_file": registry}))
    async with db_session.session_scope() as session:
        session.add(
            HistoricalJob(
                id="orphan01",
                status="RUNNING",
                slot="historical",
                owner_id="previous-process",
                actor="owner",
                recording_sha256="a" * 64,
                progress_pct=25,
                published=False,
                updated_at=fake_clock.utcnow(),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        assert (await client.get("/api/v1/historical-jobs")).status_code == 401
        login = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": password}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        plans = await client.get("/api/v1/historical-jobs/plans")
        assert plans.json() == [
            {"id": "fixture01", "label": "Isolated deterministic fixture", "simulated": True}
        ]
        body = {
            "plan_id": "fixture01",
            "reason": "Owner requested research",
            "confirmation": "RUN HISTORICAL PAPER",
        }
        assert (
            await client.post("/api/v1/historical-jobs", json={**body, "database_url": "secret"})
        ).status_code == 422
        assert (await client.post("/api/v1/historical-jobs", json=body)).status_code == 409
        rows = (await client.get("/api/v1/historical-jobs")).json()
        assert rows[0]["liveness"] == "UNVERIFIED_OWNER_REVIEW_REQUIRED"
        assert rows[0]["simulated"] is True
        assert rows[0]["progress_pct"] == "25.000000"
        assert not app.state.historical_jobs.tasks


async def test_immediate_shutdown_releases_only_owned_unstarted_job(
    isolated_database,
    catalog_database,
    tmp_path,
    monkeypatch,
):
    registry = plan_files(tmp_path, isolated_database)
    select_target(monkeypatch, catalog_database)
    manager = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    await manager.enqueue("fixture01", actor="owner", reason="Owner requested research")
    await manager.stop()
    async with db_session.session_scope() as session:
        row = await session.get(HistoricalJob, "fixture01")
        assert row.status == "INTERRUPTED" and row.slot is None
        assert not row.published


@pytest.mark.parametrize("failure", ["timeout", "source_schema_missing", "audit_unavailable"])
async def test_job_failure_never_publishes_success(
    isolated_database,
    catalog_database,
    tmp_path,
    monkeypatch,
    failure,
):
    registry = plan_files(tmp_path, isolated_database)
    if failure == "source_schema_missing":
        async with isolated_database.begin() as connection:
            await connection.run_sync(HistoricalJob.__table__.drop)
    if failure == "timeout":
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["plans"][0]["timeout_seconds"] = 1
        registry.write_text(json.dumps(payload), encoding="utf-8")
    select_target(monkeypatch, catalog_database)
    manager = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    if failure == "audit_unavailable":

        async def fail_audit(*args, **kwargs):
            raise RuntimeError("isolated audit failure")

        monkeypatch.setattr(AuditService, "append_in_session", fail_audit)
        with pytest.raises(RuntimeError, match="audit failure"):
            await manager.enqueue("fixture01", actor="owner", reason="Explicit research request")
        async with db_session.session_scope() as session:
            assert await session.get(HistoricalJob, "fixture01") is None
        assert not manager.tasks
        return
    try:
        await manager.enqueue("fixture01", actor="owner", reason="Explicit research request")
        await asyncio.wait_for(asyncio.shield(manager.tasks["fixture01"]), 30)
        async with db_session.session_scope() as session:
            row = await session.get(HistoricalJob, "fixture01")
            assert row.status == "FAILED" and not row.published
            assert row.slot is None and row.error_code
            assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestResult)) == 0
    finally:
        await manager.stop()
