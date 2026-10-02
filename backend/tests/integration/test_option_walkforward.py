"""Actual option training/OOS child processes retain the declared strategy identity."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.backtest.jobs import HistoricalJobs
from app.backtest.walkforward import WalkForwardJobs
from app.backtest.wf_inputs import freeze_experiment
from app.config import get_settings
from app.db import session as db_session
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob
from app.marketdata.recordings import RecordingBundle, read_recording, write_recording
from tests.integration.test_historical_options import recorded_option
from tests.integration.test_walkforward import experiment_files


@pytest.mark.parametrize("oos_target", [False, True])
async def test_option_training_only_selection_and_attributed_oos(
    db_engine, fake_clock, tmp_path, oos_target
):
    registry, experiment = await experiment_files(tmp_path, recorded=recorded_option())
    if oos_target:
        for pair in experiment.candidates[0]:
            recording = read_recording(tmp_path / pair.test_plan / "recording.json")
            snapshots = []
            for row in recording.snapshots:
                snapshot = row
                if row.kind == "QUOTE" and row.available_at > experiment.windows()[0].test_start:
                    quote = row.value
                    snapshot = row.model_copy(
                        update={
                            "value": replace(
                                quote,
                                ltp=Decimal(400),
                                bids=(replace(quote.bids[0], price=Decimal(400)),),
                                asks=(replace(quote.asks[0], price=Decimal("400.05")),),
                            )
                        }
                    )
                snapshots.append(snapshot)
            replace_recording(
                registry,
                pair.test_plan,
                recording.model_copy(update={"snapshots": tuple(snapshots)}),
            )
    jobs = HistoricalJobs(get_settings().model_copy(update={"historical_plans_file": registry}))
    controller = WalkForwardJobs(jobs)
    try:
        await controller.enqueue(
            experiment.id, actor="owner", reason="Synthetic option OOS fixture"
        )
        await asyncio.wait_for(asyncio.shield(controller.tasks[experiment.id]), 120)
        async with db_session.session_scope() as session:
            state = await session.get(WalkForwardJob, experiment.id)
            assert state.status == "COMPLETED", state.error_code
            runs = list((await session.scalars(sa.select(BacktestRun))).all())
            assert len(runs) == 4
            assert all(run.strategy_id == "long-option-breakout" for run in runs)
            assert all(run.simulated for run in runs)
            parent = next(run for run in runs if run.id == experiment.id)
            windows = parent.assumptions["window_disclosure"]["windows"]
            assert len(windows) == 1 and windows[0]["source_run_id"] == "test00001"
            assert windows[0]["role"] == "OUT_OF_SAMPLE"
            assert windows[0]["span_state"] == "BELOW_MINIMUM"
            assert windows[0]["elapsed_seconds"] == "59.999999"
            results = list(
                (
                    await session.scalars(
                        sa.select(BacktestResult).where(
                            BacktestResult.run_id == experiment.id,
                        )
                    )
                ).all()
            )
            window = next(row for row in results if row.window_kind == "OUT_OF_SAMPLE")
            aggregate = next(row for row in results if row.window_kind == "OOS_AGGREGATE")
            selection = window.window_parameters["selected_training_score"]
            assert selection["candidate"] == "candidate-1"
            assert await session.get(HistoricalJob, "test00002") is None
            assert window.gross_pnl == (Decimal(600) if oos_target else Decimal(-8))
            assert window.total_charges > 0 and window.net_pnl < window.gross_pnl
            assert aggregate.trade_count == 1 and aggregate.net_pnl == window.net_pnl
            assert aggregate.total_return is None and aggregate.max_drawdown is None
            binding = window.window_parameters["diagnostics"]["strategy_binding"]
            assert binding["specification"]["id"] == "long-option-breakout"
            trades = list(
                (
                    await session.scalars(
                        sa.select(BacktestTrade).where(
                            BacktestTrade.run_id == experiment.id,
                        )
                    )
                ).all()
            )
            assert len(trades) == 1 and trades[0].quantity == 2
            assert trades[0].entry_price == 100
            assert trades[0].exit_price == (400 if oos_target else 96)
        audit = AuditService(fake_clock)
        assert await audit.verify(f"wf:{experiment.id}")
        events = await audit.chain(f"wf:{experiment.id}")
        types = [event.event_type for event in events]
        assert types.index("WALKFORWARD_SELECTION_FROZEN") < types.index("WALKFORWARD_OOS_RECORDED")
        restarted = WalkForwardJobs(jobs)
        assert (
            await restarted.enqueue(experiment.id, actor="owner", reason="Restart duplicate")
            == experiment.id
        )
        assert not restarted.tasks
    finally:
        await controller.stop()
        await jobs.stop()


async def test_option_experiment_requires_oos_chain_before_child_execution(tmp_path):
    registry, experiment = await experiment_files(tmp_path, recorded=recorded_option())
    identifier = experiment.candidates[0][0].test_plan
    directory = tmp_path / identifier
    payload = read_recording(directory / "recording.json").model_dump()
    payload["snapshots"] = [row for row in payload["snapshots"] if row["kind"] != "CHAIN"]
    recording = RecordingBundle.model_validate(payload)
    replace_recording(registry, identifier, recording)
    with pytest.raises(ValueError, match="OPTION_CHAIN_HISTORY_UNAVAILABLE"):
        freeze_experiment(registry, experiment)


def replace_recording(registry, identifier, recording):
    directory = registry.parent / identifier
    write_recording(directory / "altered-recording.json", recording)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["recording_sha256"] = recording.content_hash()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    plans = json.loads(registry.read_text(encoding="utf-8"))
    selected = next(plan for plan in plans["plans"] if plan["id"] == identifier)
    selected["recording"] = f"{identifier}/altered-recording.json"
    registry.write_text(json.dumps(plans), encoding="utf-8")
