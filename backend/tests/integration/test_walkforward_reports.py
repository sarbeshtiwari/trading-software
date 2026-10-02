"""Independent expected values for two real OOS windows, drift and audit-bound API reads."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.backtest.windows import DegradationPolicy, WalkForwardPlan, rolling_windows
from app.config import get_settings
from app.db import session as db_session
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.db.models.walkforward import WalkForwardJob
from app.main import create_app
from app.strategies.base import StrategySpec
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry
from tests.integration.test_auth import credentials
from tests.integration.test_historical_api import login
from tests.integration.test_walkforward import experiment_files

__all__ = ["credentials"]


async def two_windows(root):
    plans, candidates, experiments = [], [], []
    for index, folder in enumerate(("first", "second")):
        directory = root / folder
        directory.mkdir()
        path, experiment = await experiment_files(directory, index=index, invert=bool(index))
        payload = json.loads(path.read_text(encoding="utf-8"))
        for plan in payload["plans"]:
            plans.append(
                plan
                | {key: f"{folder}/{plan[key]}" for key in ("manifest", "recording", "settings")}
            )
        candidates.append(experiment.candidates[0])
        experiments.append(experiment)
    experiment = WalkForwardPlan.model_validate(
        experiments[0].model_dump()
        | {
            "end_at": experiments[1].end_at,
            "step_seconds": 3720,
            "candidates": candidates,
            "degradation": {"max_net_return_drop": "0.005"},
            "validation": {
                "minimum_trades": 2,
                "minimum_windows": 2,
                "minimum_expectancy": "0",
                "maximum_within_window_drawdown": "0.02",
                "minimum_profitable_window_fraction": "0.5",
            },
        }
    )
    registry = root / "plans.json"
    registry.write_text(
        json.dumps({"plans": plans, "experiments": [experiment.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    return registry, experiment


async def test_two_actual_windows_aggregate_only_oos_and_expose_drift_and_tampering(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    monkeypatch,
):
    registry, experiment = await two_windows(tmp_path)
    app = create_app(get_settings().model_copy(update={"historical_plans_file": registry}))
    controller = app.state.walkforward_jobs
    try:
        await controller.enqueue(
            experiment.id, actor="owner", reason="Two-window isolated regression"
        )
        await asyncio.wait_for(asyncio.shield(controller.tasks[experiment.id]), 300)
        async with db_session.session_scope() as session:
            run = await session.get(BacktestRun, experiment.id)
            assert run.status == "COMPLETED", run.error_detail
            job = await session.get(WalkForwardJob, experiment.id)
            disclosure = run.assumptions["universe_disclosure"]
            assert len(disclosure["windows"]) == len(job.frozen_inputs)
            assert {window["source_run_id"] for window in disclosure["windows"]} == {
                value["manifest"]["run_id"] for value in job.frozen_inputs.values()
            }
            assert disclosure["historical_eligibility_verified"] is False
            results = list(
                (
                    await session.scalars(
                        sa.select(BacktestResult).where(BacktestResult.run_id == experiment.id)
                    )
                ).all()
            )
            aggregate = next(row for row in results if row.window_kind == "OOS_AGGREGATE")
            windows = sorted(
                (row for row in results if row.window_kind == "OUT_OF_SAMPLE"),
                key=lambda row: row.window_index,
            )
            assert [row.net_pnl for row in windows] == [Decimal("-473.48"), Decimal("424.38")]
            assert [row.overfitting_flag for row in windows] == [True, False]
            assert aggregate.gross_pnl == -4 and aggregate.total_charges == Decimal("45.10")
            assert aggregate.net_pnl == Decimal("-49.10") and aggregate.trade_count == 2
            assert aggregate.win_rate == Decimal("0.5") and aggregate.expectancy == Decimal(
                "-24.55"
            )
            assert aggregate.total_return is None and aggregate.max_drawdown is None
            assert aggregate.overfitting_flag is True
            diagnostics = aggregate.window_parameters["diagnostics"]
            assert diagnostics["candidate_choices"] == ["candidate-2", "candidate-1"]
            assert diagnostics["parameter_changes"] == 1
            assert diagnostics["binding_summary"]["state"] == "MIXED"
            assert len(diagnostics["binding_summary"]["parameter_hashes"]) == 2
            assert Decimal(diagnostics["absolute_risk_fraction_drift"]) == Decimal("0.0025")
            assert Decimal(diagnostics["profitable_window_fraction"]) == Decimal("0.5")
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(BacktestTrade)
                    .where(BacktestTrade.run_id == experiment.id)
                )
                == 2
            )
            aggregate_id = aggregate.id
        assert await AuditService(fake_clock).verify(f"wf:{experiment.id}")
        fake_clock.set_to(experiment.end_at + timedelta(seconds=1))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Requested-With": "ATS"},
        ) as client:
            review_path = f"/api/v1/backtests/{experiment.id}/review"
            body = {
                "reason": "Review persisted OOS evidence",
                "confirmation": "REVIEW OOS EVIDENCE",
            }
            assert (await client.post(review_path, json=body)).status_code == 401
            await login(client, credentials[1])
            detail = (await client.get(f"/api/v1/backtests/{experiment.id}")).json()
            assert detail["catalog_integrity"] == "AUDIT_BOUND"
            assert detail["universe_disclosure"] == disclosure
            downloaded = await client.get(f"/api/v1/backtests/{experiment.id}/export")
            assert downloaded.status_code == 200, downloaded.text
            exported = downloaded.json()
            assert exported["simulated"] is True
            assert exported["universe_disclosure"] == disclosure
            assert len(exported["trades"]) == 2
            assert all(
                trade["source_run_id"] and trade["source_trade_id"] for trade in exported["trades"]
            )
            assert detail["evidence_review"]["status"] == "FAILED"
            assert detail["evidence_review"]["failures"] == ["EXPECTANCY_BELOW_THRESHOLD"]
            assert (
                await client.post(review_path, json=body | {"minimum_expectancy": "-100"})
            ).status_code == 422
            reviewed = await client.post(review_path, json=body)
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["live_approved"] is False
            assert reviewed.json()["simulated"] is True
            assert reviewed.json()["audit_event_id"]
            reviews = await AuditService(fake_clock).chain(f"validation:{experiment.id}")
            assert reviews[-1].actor == "owner"
            assert reviews[-1].result["review"]["status"] == "FAILED"
            assert await AuditService(fake_clock).verify(f"validation:{experiment.id}")
            original_append = AuditService.append_in_session

            async def failed_review_audit(service, session, identity, details, **kwargs):
                if identity.event_type == "OOS_EVIDENCE_REVIEWED":
                    raise RuntimeError("isolated review audit failure")
                return await original_append(service, session, identity, details, **kwargs)

            with monkeypatch.context() as context:
                context.setattr(AuditService, "append_in_session", failed_review_audit)
                with pytest.raises(RuntimeError, match="isolated review audit failure"):
                    await client.post(review_path, json=body)
            assert len(await AuditService(fake_clock).chain(f"validation:{experiment.id}")) == 1
            summary = next(
                row for row in detail["results"] if row["window_kind"] == "OOS_AGGREGATE"
            )
            assert summary["diagnostics"]["degradation_state"] == "FLAGGED"
            assert summary["diagnostics"]["binding_summary"]["state"] == "MIXED"
            window = next(
                item for item in detail["results"] if item["window_kind"] == "OUT_OF_SAMPLE"
            )
            spec = StrategySpec.model_validate(
                window["diagnostics"]["strategy_binding"]["specification"]
            )
            await StrategyRegistry().register(
                ClosedCandleBreakout(
                    "first", spec.universe[0], "0.05", spec.risk.requested_risk_fraction
                )
            )
            linked = await client.post(
                "/api/v1/strategies/closed-candle-breakout/evidence/review",
                json={
                    "version": "1",
                    "backtest_id": window["diagnostics"]["source_run_id"],
                    "walkforward_id": experiment.id,
                    "reason": "Check immutable report linkage",
                    "confirmation": "REVIEW STRATEGY EVIDENCE",
                },
            )
            assert linked.status_code == 200, linked.text
            assert linked.json()["report_states"]["backtest"].startswith(
                "AUDIT_BOUND_PARAMETERS_MATCH"
            )
            assert linked.json()["report_states"]["walkforward"] == "MIXED_OR_MISMATCHED_PARAMETERS"
            assert linked.json()["live_approved"] is False
            trades = (await client.get(f"/api/v1/backtests/{experiment.id}/trades")).json()[
                "trades"
            ]
            assert [trade["window_index"] for trade in trades] == [0, 1]
            assert [trade["quantity"] for trade in trades] == [111, 55]
            for trade in trades:
                original = (
                    await client.get(f"/api/v1/backtests/{trade['source_run_id']}/trades")
                ).json()["trades"]
                assert trade["source_trade_id"] in {row["id"] for row in original}
            async with db_session.session_scope() as session:
                (await session.get(BacktestResult, aggregate_id)).net_pnl += 1
            assert (await client.post(review_path, json=body)).status_code == 409
            for suffix in ("", "/trades", "/samples"):
                response = await client.get(f"/api/v1/backtests/{experiment.id}{suffix}")
                assert response.status_code == 409 and "integrity" in response.text
    finally:
        await controller.stop()
        await app.state.historical_jobs.stop()


def test_duration_arithmetic_uses_instants_across_a_repeated_wall_clock_hour():
    timezone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0)
    end = datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=1)
    windows = rolling_windows(start, end, train_seconds=1800, test_seconds=1800, step_seconds=1800)
    assert len(windows) == 1 and windows[0].test_end - windows[0].train_start == timedelta(hours=1)
    with pytest.raises(ValueError):
        DegradationPolicy(max_net_return_drop="NaN")


@pytest.mark.parametrize("failure", ["audit", "accounting"])
async def test_report_finalization_fails_closed(db_engine, tmp_path, monkeypatch, failure):
    registry, experiment = await experiment_files(tmp_path)
    app = create_app(get_settings().model_copy(update={"historical_plans_file": registry}))
    controller = app.state.walkforward_jobs
    original = controller.audit

    async def inject_failure(session, identifier, event, actor, result, **kwargs):
        if event == "WALKFORWARD_COMPLETED" and failure == "audit":
            raise RuntimeError("isolated final audit failure")
        await original(session, identifier, event, actor, result, **kwargs)
        if event == "WALKFORWARD_OOS_RECORDED" and failure == "accounting":
            await session.flush()
            row = await session.scalar(
                sa.select(BacktestResult).where(BacktestResult.run_id == identifier)
            )
            row.net_pnl += 1

    monkeypatch.setattr(controller, "audit", inject_failure)
    try:
        await controller.enqueue(experiment.id, actor="owner", reason="Finalization failure test")
        await asyncio.wait_for(asyncio.shield(controller.tasks[experiment.id]), 180)
        async with db_session.session_scope() as session:
            run = await session.get(BacktestRun, experiment.id)
            job = await session.get(WalkForwardJob, experiment.id)
            assert run.status == job.status == "FAILED"
            assert job.slot is None
            rows = list(
                (
                    await session.scalars(
                        sa.select(BacktestResult).where(BacktestResult.run_id == experiment.id)
                    )
                ).all()
            )
            assert len(rows) == 1 and rows[0].window_kind == "OUT_OF_SAMPLE"
        chain = await AuditService(controller.clock).chain(f"wf:{experiment.id}")
        assert not any(event.event_type == "WALKFORWARD_COMPLETED" for event in chain)
    finally:
        await controller.stop()
        await app.state.historical_jobs.stop()
