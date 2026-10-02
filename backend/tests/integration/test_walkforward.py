"""Real training and OOS child processes; only external market observations are fixtures."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.audit.service import AuditService
from app.backtest.bootstrap import HistoricalManifest
from app.backtest.jobs import HistoricalJobs
from app.backtest.walkforward import WalkForwardJobs
from app.backtest.wf_inputs import (
    TrainingScore,
    check_recording_consistency,
    freeze_experiment,
    select_candidate,
)
from app.backtest.windows import WalkForwardPlan, rolling_windows
from app.config import get_settings
from app.db import session as db_session
from app.db.base import Base
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob
from app.main import create_app
from app.marketdata.recordings import RecordingBundle, write_recording
from tests.integration.test_auth import credentials
from tests.integration.test_historical_api import login
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["credentials"]


def shifted(value, delta):
    if isinstance(value, datetime):
        return value + delta
    if isinstance(value, dict):
        return {key: shifted(item, delta) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [shifted(item, delta) for item in value]
    return value


async def experiment_files(root, *, index=0, invert=False, recorded=None):
    original, base = recorded or recorded_trade()
    plans = []
    pairs = []
    for number, fraction in enumerate(("0.0025", "0.005"), start=1):
        pair = {
            "name": f"candidate-{number}",
            "train_plan": f"train{index:02}{number:02}",
            "test_plan": f"test{index:03}{number:02}",
        }
        pairs.append(pair)
        for role, offset in (("train", 0), ("test", 1860)):
            identifier = pair[f"{role}_plan"]
            directory = root / identifier
            directory.mkdir()
            delta = timedelta(seconds=offset + index * 3720)
            recorded = shifted(original.model_dump(), delta)
            exit_snapshot = recorded["snapshots"][-1]
            recorded["snapshots"] = [
                *recorded["snapshots"][:-1],
                *[
                    shifted(exit_snapshot, timedelta(seconds=seconds - 5))
                    for seconds in range(5, 60, 5)
                ],
            ]
            if (role == "test") != invert:
                for snapshot in recorded["snapshots"][1:]:
                    if snapshot["kind"] != "QUOTE":
                        continue
                    quote = snapshot["value"]
                    quote["ltp"] = Decimal(96)
                    quote["bids"][0]["price"] = Decimal(96)
                    quote["asks"][0]["price"] = Decimal("96.05")
            recording = RecordingBundle.model_validate(recorded)
            payload = shifted(base.model_dump(), delta)
            payload.update(
                run_id=identifier,
                recording_sha256=recording.content_hash(),
                end_at=base.start_at + delta + timedelta(seconds=60, microseconds=-1),
                strategy_risk_fraction=fraction,
            )
            manifest = HistoricalManifest.model_validate(payload)
            source = create_async_engine(
                f"sqlite+aiosqlite:///{directory / ('ats_history_' + identifier + '.db')}"
            )
            try:
                async with source.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                (directory / "settings.json").write_text(
                    json.dumps({"database_url": str(source.url), "starting_capital": "100000"}),
                    encoding="utf-8",
                )
            finally:
                await source.dispose()
            (directory / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
            write_recording(directory / "recording.json", recording)
            plans.append(
                {
                    "id": identifier,
                    "label": f"Fixture {identifier}",
                    "manifest": f"{identifier}/manifest.json",
                    "recording": f"{identifier}/recording.json",
                    "settings": f"{identifier}/settings.json",
                    "timeout_seconds": 90,
                }
            )
    experiment = WalkForwardPlan(
        id=f"walktest{index + 1:02}",
        label="Isolated walk-forward fixture",
        start_at=base.start_at + timedelta(seconds=index * 3720),
        end_at=base.start_at + timedelta(seconds=index * 3720 + 1920),
        train_seconds=60,
        test_seconds=60,
        step_seconds=60,
        embargo_seconds=1800,
        minimum_training_trades=1,
        candidates=[pairs],
    )
    registry = root / "plans.json"
    registry.write_text(
        json.dumps({"plans": plans, "experiments": [experiment.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    return registry, experiment


async def test_training_only_selection_then_real_oos_loss_and_api_report(
    db_engine, credentials, fake_clock, tmp_path
):
    registry, experiment = await experiment_files(tmp_path)
    settings = get_settings().model_copy(update={"historical_plans_file": registry})
    app = create_app(settings)
    controller = app.state.walkforward_jobs
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Requested-With": "ATS"},
        ) as client:
            assert (await client.get("/api/v1/historical-jobs/experiments")).status_code == 401
            await login(client, credentials[1])
            response = await client.post(
                "/api/v1/historical-jobs/experiments",
                json={
                    "plan_id": experiment.id,
                    "reason": "Isolated chronological research fixture",
                    "confirmation": "RUN WALK FORWARD",
                },
            )
            assert response.status_code == 202, response.text
            await asyncio.wait_for(asyncio.shield(controller.tasks[experiment.id]), 120)
            async with db_session.session_scope() as session:
                job = await session.get(WalkForwardJob, experiment.id)
                child_states = list(
                    (
                        await session.execute(
                            sa.select(
                                HistoricalJob.id, HistoricalJob.status, HistoricalJob.error_code
                            )
                        )
                    ).all()
                )
                reports = list(
                    (
                        await session.execute(
                            sa.select(BacktestResult.run_id, BacktestResult.notes)
                        )
                    ).all()
                )
                assert job.status == "COMPLETED", (job.error_code, child_states, reports)
                assert job.slot is None and job.progress_pct == 100
                results = list(
                    (
                        await session.scalars(
                            sa.select(BacktestResult).where(BacktestResult.run_id == experiment.id)
                        )
                    ).all()
                )
                windows = [row for row in results if row.window_kind == "OUT_OF_SAMPLE"]
                aggregate = next(row for row in results if row.window_kind == "OOS_AGGREGATE")
                assert len(windows) == 1
                assert (
                    windows[0].window_parameters["selected_training_score"]["candidate"]
                    == "candidate-2"
                )
                assert windows[0].gross_pnl == -444 and windows[0].net_pnl < -444
                assert aggregate.trade_count == 1 and aggregate.net_pnl == windows[0].net_pnl
                assert aggregate.max_drawdown is None and aggregate.total_return is None
                assert (
                    aggregate.window_parameters["diagnostics"]["binding_summary"]["state"]
                    == "FIXED"
                )
                trades = list(
                    (
                        await session.scalars(
                            sa.select(BacktestTrade).where(BacktestTrade.run_id == experiment.id)
                        )
                    ).all()
                )
                assert len(trades) == 1 and trades[0].quantity == 111
                assert await session.get(HistoricalJob, "test00001") is None
                assert (await session.get(BacktestRun, experiment.id)).kind == "WALKFORWARD"
            detail = (await client.get(f"/api/v1/backtests/{experiment.id}")).json()
            assert len(detail["results"]) == 2 and detail["run"]["simulated"]
            assert all(Decimal(item["net_pnl"]) < 0 for item in detail["results"])
            audit = AuditService(fake_clock)
            assert await audit.verify(f"wf:{experiment.id}")
            chain = await audit.chain(f"wf:{experiment.id}")
            types = [event.event_type for event in chain]
            assert types.index("WALKFORWARD_SELECTION_FROZEN") < types.index(
                "WALKFORWARD_OOS_RECORDED"
            )
            restarted = WalkForwardJobs(HistoricalJobs(settings))
            assert (
                await restarted.enqueue(experiment.id, actor="owner", reason="Duplicate experiment")
                == experiment.id
            )
            assert not restarted.tasks
    finally:
        await controller.stop()
        await app.state.historical_jobs.stop()


def test_windows_are_half_open_chronological_and_nonoverlapping():
    _, base = recorded_trade()
    start = base.start_at
    windows = rolling_windows(
        start,
        start + timedelta(days=10),
        train_seconds=3 * 86400,
        test_seconds=2 * 86400,
        step_seconds=2 * 86400,
    )
    assert [
        (
            int((item.train_start - start).total_seconds() / 86400),
            int((item.test_start - start).total_seconds() / 86400),
            int((item.test_end - start).total_seconds() / 86400),
        )
        for item in windows
    ] == [(0, 3, 5), (2, 5, 7), (4, 7, 9)]
    with pytest.raises(ValueError, match="overlap"):
        rolling_windows(
            start, start + timedelta(days=10), train_seconds=3, test_seconds=2, step_seconds=1
        )


@pytest.mark.parametrize("failure", ["changed_input", "insufficient_training", "selection_audit"])
async def test_failed_experiment_never_executes_oos(db_engine, tmp_path, monkeypatch, failure):
    registry, plan = await experiment_files(tmp_path)
    if failure == "insufficient_training":
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["experiments"][0]["minimum_training_trades"] = 2
        registry.write_text(json.dumps(payload), encoding="utf-8")
    if failure == "selection_audit":
        original = AuditService.append_in_session

        async def failing_selection(self, session, identity, details, **kwargs):
            if identity.event_type == "WALKFORWARD_SELECTION_FROZEN":
                raise RuntimeError("isolated selection audit failure")
            return await original(self, session, identity, details, **kwargs)

        monkeypatch.setattr(AuditService, "append_in_session", failing_selection)
    historical = HistoricalJobs(
        get_settings().model_copy(update={"historical_plans_file": registry})
    )
    controller = WalkForwardJobs(historical)
    try:
        await controller.enqueue(plan.id, actor="owner", reason="Adversarial isolated experiment")
        if failure == "changed_input":
            path = tmp_path / "train0001" / "manifest.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["strategy_risk_fraction"] = "0.004"
            path.write_text(json.dumps(payload), encoding="utf-8")
        await asyncio.wait_for(asyncio.shield(controller.tasks[plan.id]), 300)
        async with db_session.session_scope() as session:
            job = await session.get(WalkForwardJob, plan.id)
            assert job.status == "FAILED" and job.slot is None
            assert await session.get(HistoricalJob, "test00001") is None
            assert await session.get(HistoricalJob, "test00002") is None
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(BacktestTrade)
                    .where(BacktestTrade.run_id == plan.id)
                )
                == 0
            )
    finally:
        await controller.stop()
        await historical.stop()


async def test_freeze_refuses_parameter_changes_between_training_and_oos(db_engine, tmp_path):
    registry, plan = await experiment_files(tmp_path)
    path = tmp_path / "test00002" / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["strategy_risk_fraction"] = "0.004"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen"):
        freeze_experiment(registry, plan)


def test_optimizer_contract_accepts_training_scores_only_and_breaks_ties_stably():
    first = TrainingScore(
        candidate="first",
        train_run_id="train0001",
        net_return="0.01",
        trade_count=2,
        outcome_sha256="a" * 64,
    )
    second = first.model_copy(update={"candidate": "second", "train_run_id": "train0002"})
    assert select_candidate((second, first), 2) == first
    with pytest.raises(ValueError):
        TrainingScore.model_validate(first.model_dump() | {"out_of_sample_return": "100"})
    with pytest.raises(ValueError, match="no eligible"):
        select_candidate((first,), 3)


def test_conflicting_historical_observations_cannot_be_used_for_different_windows():
    recording, _ = recorded_trade()
    observations = {}
    check_recording_consistency(observations, recording)
    check_recording_consistency(observations, recording)
    changed = recording.model_dump()
    changed["candles"][0]["bars"][0]["volume"] += 1
    with pytest.raises(ValueError, match="conflicting"):
        check_recording_consistency(observations, RecordingBundle.model_validate(changed))
