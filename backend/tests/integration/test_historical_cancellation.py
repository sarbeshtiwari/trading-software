"""Authenticated cancellation reaps actual research children, not broker mocks."""

import asyncio

import httpx
import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.backtest import launcher
from app.backtest.cancellation import cancel_owned
from app.backtest.jobs import HistoricalJobs
from app.backtest.walkforward import WalkForwardJobs
from app.backtest.wf_inputs import freeze_experiment
from app.config import get_settings
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob
from app.main import create_app
from tests.integration.test_auth import credentials
from tests.integration.test_historical_api import login
from tests.integration.test_walkforward import experiment_files

__all__ = ["credentials"]

BODY = {"reason": "Owner stopped fixture research", "confirmation": "CANCEL HISTORICAL RESEARCH"}


async def test_disconnected_caller_does_not_abandon_controller_cleanup(
    db_engine, tmp_path, monkeypatch
):
    registry, _ = await experiment_files(tmp_path)
    manager = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    cleanup_entered, release = asyncio.Event(), asyncio.Event()
    original = manager.finish_cancelled

    async def delayed_cleanup(*args, **kwargs):
        cleanup_entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "finish_cancelled", delayed_cleanup)
    await manager.enqueue("train0001", actor="owner", reason="Disconnected caller test")
    child = manager.tasks["train0001"]
    request = asyncio.create_task(
        cancel_owned(
            manager, "train0001", HistoricalJob, "hjob", actor="owner", reason=BODY["reason"]
        )
    )
    entered = asyncio.create_task(cleanup_entered.wait())
    try:
        done, _ = await asyncio.wait(
            {entered, request}, timeout=30, return_when=asyncio.FIRST_COMPLETED
        )
        if request in done:
            await request
        assert entered in done, "controller cleanup was not reached"
        operation = manager.cancellations["train0001"]
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        assert not operation.done()
        assert child.done()
        release.set()
        assert await asyncio.wait_for(asyncio.shield(operation), 30) == "INTERRUPTED"
        async with db_session.session_scope() as session:
            row = await session.get(HistoricalJob, "train0001")
            assert row.slot is None and row.error_code == "OWNER_CANCELLED"
    finally:
        release.set()
        entered.cancel()
        await asyncio.gather(entered, return_exceptions=True)
        await manager.stop()


async def test_cancellation_during_child_reservation_does_not_detach_process(
    db_engine, tmp_path, monkeypatch
):
    registry, plan = await experiment_files(tmp_path)
    jobs = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    controller = WalkForwardJobs(jobs)
    frozen = freeze_experiment(registry, plan)
    release = asyncio.Event()
    launched = asyncio.Event()
    processes = []
    original_enqueue = jobs.enqueue_prepared
    original_launch = launcher.asyncio.create_subprocess_exec

    async def delayed_return(*args, **kwargs):
        result = await original_enqueue(*args, **kwargs)
        await release.wait()
        return result

    async def observed_launch(*args, **kwargs):
        process = await original_launch(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(jobs, "enqueue_prepared", delayed_return)
    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", observed_launch)
    child = asyncio.create_task(controller._child("train0001", frozen, "walkforward-training"))
    try:
        await asyncio.wait_for(launched.wait(), 30)
        child.cancel("OWNER_CANCELLED")
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(child, return_exceptions=True)
        assert processes and all(process.returncode is not None for process in processes)
        async with db_session.session_scope() as session:
            row = await session.get(HistoricalJob, "train0001")
            assert row.slot is None and row.status == "INTERRUPTED"
    finally:
        release.set()
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await jobs.stop()


@pytest.mark.parametrize("experiment", [False, True])
async def test_owner_cancellation_reaps_child_and_persists_audit(
    db_engine, credentials, tmp_path, monkeypatch, fake_clock, experiment
):
    registry, plan = await experiment_files(tmp_path)
    app = create_app(credentials[0].model_copy(update={"historical_plans_file": registry}))
    jobs, experiments = app.state.historical_jobs, app.state.walkforward_jobs
    manager = experiments if experiment else jobs
    identifier = plan.id if experiment else "train0001"
    resource = "/api/v1/historical-jobs" + ("/experiments" if experiment else "")
    model, prefix = (WalkForwardJob, "wf") if experiment else (HistoricalJob, "hjob")
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Requested-With": "ATS"},
        ) as client:
            endpoint = f"{resource}/{identifier}/cancel"
            assert (await client.post(endpoint, json=BODY)).status_code == 401
            await login(client, credentials[1])
            await manager.enqueue(identifier, actor="owner", reason="Actual child cancellation")
            await asyncio.wait_for(launched.wait(), 30)
            assert processes[0].returncode is None
            rows = (await client.get(resource)).json()
            assert rows[0]["cancellable"] is True
            with monkeypatch.context() as fault:

                async def unavailable_audit(*args, **kwargs):
                    raise RuntimeError("isolated cancellation audit failure")

                fault.setattr(AuditService, "append_in_session", unavailable_audit)
                assert (await client.post(endpoint, json=BODY)).status_code == 409
                assert processes[0].returncode is None
            assert (
                await client.post(endpoint, json={**BODY, "confirmation": "yes"})
            ).status_code == 422
            if experiment:
                child_endpoint = "/api/v1/historical-jobs/train0001/cancel"
                assert (await client.post(child_endpoint, json=BODY)).status_code == 409
                assert processes[0].returncode is None
            responses = await asyncio.gather(
                client.post(endpoint, json=BODY), client.post(endpoint, json=BODY)
            )
            assert all(response.status_code == 200 for response in responses)
            assert all(response.json()["status"] == "INTERRUPTED" for response in responses)
            assert processes and all(process.returncode is not None for process in processes)
            assert (await client.post(endpoint, json=BODY)).json()["status"] == "INTERRUPTED"
            rows = (await client.get(resource)).json()
            assert rows[0]["liveness"] == "TERMINAL"
            assert rows[0]["cancellable"] is False
            assert not rows[0]["published"]
        async with db_session.session_scope() as session:
            row = await session.get(model, identifier)
            assert row.status == "INTERRUPTED" and row.slot is None
            assert row.error_code == "OWNER_CANCELLED"
            child = await session.get(HistoricalJob, "train0001")
            assert child.slot is None and not child.published
            assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestResult)) == 0
            events = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.chain_id == f"{prefix}:{identifier}",
                            AuditEvent.event_type == "HISTORICAL_CANCEL_REQUESTED",
                        )
                    )
                ).all()
            )
            assert len(events) == 1 and events[0].actor == "owner"
            assert events[0].result["reason"] == BODY["reason"]
        assert await AuditService(fake_clock).verify(f"{prefix}:{identifier}")
    finally:
        await experiments.stop()
        await jobs.stop()


@pytest.mark.parametrize("experiment", [False, True])
async def test_cancellation_never_releases_unknown_process_reservation(
    db_engine, credentials, fake_clock, experiment
):
    app = create_app(credentials[0])
    model = WalkForwardJob if experiment else HistoricalJob
    fields = (
        {"specification": {}, "frozen_inputs": {}}
        if experiment
        else {
            "recording_sha256": "a" * 64,
            "published": False,
        }
    )
    async with db_session.session_scope() as session:
        session.add(
            model(
                id="orphan01",
                status="RUNNING",
                slot="reserved",
                owner_id="old-process",
                actor="owner",
                progress_pct=0,
                updated_at=fake_clock.utcnow(),
                **fields,
            )
        )
    resource = "/api/v1/historical-jobs" + ("/experiments" if experiment else "")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        await login(client, credentials[1])
        assert (await client.post(f"{resource}/orphan01/cancel", json=BODY)).status_code == 409
        rows = (await client.get(resource)).json()
        assert rows[0]["liveness"] == "UNVERIFIED_OWNER_REVIEW_REQUIRED"
        assert rows[0]["cancellable"] is False
    async with db_session.session_scope() as session:
        assert (await session.get(model, "orphan01")).slot == "reserved"
