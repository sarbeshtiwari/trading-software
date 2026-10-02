"""Durable experiment reservation and actual child-process shutdown safety."""

import asyncio

import httpx
import pytest
import sqlalchemy as sa

from app.backtest import launcher
from app.backtest.jobs import HistoricalJobs
from app.backtest.walkforward import WalkForwardJobs
from app.config import get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.backtest import BacktestRun
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob
from app.main import create_app
from tests.integration.test_auth import credentials
from tests.integration.test_historical_api import login
from tests.integration.test_walkforward import experiment_files

__all__ = ["credentials"]


async def test_restart_keeps_unknown_experiment_reserved(db_engine, credentials, tmp_path):
    registry, plan = await experiment_files(tmp_path)
    settings = get_settings().model_copy(update={"historical_plans_file": registry})
    async with db_session.session_scope() as session:
        session.add(
            WalkForwardJob(
                id="orphanwf01",
                status="RUNNING",
                slot="walkforward",
                owner_id="previous-process",
                actor="owner",
                specification={},
                frozen_inputs={},
                progress_pct=0,
                updated_at=get_clock().utcnow(),
            )
        )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        await login(client, credentials[1])
        response = await client.get("/api/v1/historical-jobs/experiments")
        assert response.json()[0]["liveness"] == "UNVERIFIED_OWNER_REVIEW_REQUIRED"
        manager = app.state.walkforward_jobs
        assert (
            await manager.enqueue("orphanwf01", actor="owner", reason="Restart observation")
            == "orphanwf01"
        )
        with pytest.raises(sa.exc.IntegrityError):
            await manager.enqueue(plan.id, actor="owner", reason="Must not bypass reservation")
        assert not manager.tasks
        async with db_session.session_scope() as session:
            assert await session.get(BacktestRun, plan.id) is None
            assert (await session.get(WalkForwardJob, "orphanwf01")).slot == "walkforward"


async def test_controller_stop_reaps_real_child_before_releasing_slots(
    db_engine, tmp_path, monkeypatch
):
    registry, plan = await experiment_files(tmp_path)
    jobs = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    controller = WalkForwardJobs(jobs)
    launched = asyncio.Event()
    processes = []
    original = launcher.asyncio.create_subprocess_exec

    async def observed_launch(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", observed_launch)
    try:
        await controller.enqueue(
            plan.id, actor="owner", reason="Controlled process shutdown fixture"
        )
        await asyncio.wait_for(launched.wait(), 30)
        await controller.stop()
        assert processes and all(process.returncode is not None for process in processes)
        async with db_session.session_scope() as session:
            parent = await session.get(WalkForwardJob, plan.id)
            child = await session.get(HistoricalJob, "train0001")
            assert parent.status == child.status == "INTERRUPTED"
            assert parent.slot is None and child.slot is None
    finally:
        await controller.stop()
        await jobs.stop()
