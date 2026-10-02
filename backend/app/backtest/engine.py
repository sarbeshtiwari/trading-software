"""Bounded recorded timeline through an exclusively prepared production worker."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.backtest.bootstrap import EXECUTION_SETTINGS, HistoricalManifest
from app.backtest.catalog import catalog_digest, load_catalog
from app.backtest.report import json_metrics, summarise
from app.backtest.reproducibility import fingerprints
from app.backtest.storage import copy_journal_trades, sample_account, stored_samples
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun
from app.db.models.decision import ConsideredCandidate
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.modes import TradingMode
from app.trading.worker import active_worker


async def run_prepared(worker, manifest):
    manifest = HistoricalManifest.model_validate(manifest.model_dump())
    runtime = worker.reference_runtime
    engine = db_session.get_engine()
    database = engine.url.database or ""
    actual = Path(database).stem if engine.dialect.name == "sqlite" else database
    if (
        actual != f"ats_history_{manifest.run_id}"
        or active_worker() is not None
        or worker.running
        or runtime is None
        or not worker.executor.ready
        or worker.clock is not get_clock()
        or worker.clock.now() != manifest.start_at
        or worker.settings.trading_mode != TradingMode.PAPER
        or worker.settings.broker_provider.value != "paper"
        or worker.settings.starting_capital != manifest.risk.capital
        or runtime.provider.data_origin != DataOrigin.REPLAY
        or runtime.provider.recording_sha256 != manifest.recording_sha256
    ):
        raise ValueError("historical worker is not exclusively prepared for this recording")
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, manifest.run_id, with_for_update=True)
        if (
            row is None
            or row.status != "PREPARED"
            or row.parameters != manifest.model_dump(mode="json")
            or row.assumptions["execution_settings"]
            != worker.settings.model_dump(mode="json", include=set(EXECUTION_SETTINGS))
        ):
            raise ValueError("historical manifest or execution configuration changed")
        row.status = "RUNNING"
        row.started_at = worker.clock.utcnow()
        await AuditService(worker.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=f"history:{manifest.run_id}",
                event_type="HISTORICAL_RUN_STARTED",
                actor="historical-runner",
                mode=TradingMode.PAPER,
            ),
            {"result": {"run_id": manifest.run_id}},
            expected_count=0,
        )
    try:
        await runtime.provider.prime()
        await worker.start(schedule=False)
        worker.executor.gate.clear("startup")
        async with db_session.session_scope() as session:
            await sample_account(session, worker, manifest.run_id, baseline=True)
        await _timeline(worker, manifest)
        return await _finish(worker, manifest)
    except BaseException as error:
        async with db_session.session_scope() as session:
            row = await session.get(BacktestRun, manifest.run_id)
            row.status = "FAILED"
            row.error_detail = type(error).__name__
            row.finished_at = worker.clock.utcnow()
        raise
    finally:
        await worker.stop()


async def _timeline(worker, manifest):
    provider = worker.reference_runtime.provider
    cadence = timedelta(seconds=worker.settings.paper_cycle_seconds)
    while True:
        now = worker.clock.now()
        await worker.cycle()
        async with db_session.session_scope() as session:
            await sample_account(session, worker, manifest.run_id)
            row = await session.get(BacktestRun, manifest.run_id)
            row.progress_pct = (
                Decimal(str((now - manifest.start_at).total_seconds()))
                * 100
                / Decimal(str((manifest.end_at - manifest.start_at).total_seconds()))
            )
        if worker.failed or now == manifest.end_at:
            return
        moment = min(now + cadence, manifest.end_at)
        execution_at = worker.executor.broker.next_execution_at
        if execution_at is not None:
            moment = min(moment, execution_at)
        next_event = provider.next_event_at
        if next_event is not None and next_event <= moment:
            await provider.step()
        else:
            worker.clock.set_to(moment)


async def _finish(worker, manifest):
    async with db_session.session_scope() as session:
        positions = await session.scalar(
            sa.select(sa.func.count()).select_from(Position).where(Position.net_quantity != 0)
        )
        pending = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Order)
            .where(~Order.status.in_([status for status in OrderStatus if status.is_terminal]))
        )
        trades = list(
            (
                await session.scalars(
                    sa.select(JournalEntry).where(
                        JournalEntry.kind == "TRADE", JournalEntry.version == 1
                    )
                )
            ).all()
        )
        standdowns = await session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "REFERENCE_STAND_DOWN")
        )
        reasons = list((await session.scalars(sa.select(ConsideredCandidate.reason_code))).all())
        evaluations = await session.scalars(
            sa.select(AuditEvent.result).where(AuditEvent.event_type == "REFERENCE_EVALUATION")
        )
        strategy_errors = sum(
            result.get("strategy_reason")
            in {
                "STRATEGY_DATA_BOUNDARY_VIOLATION",
                "INVALID_OR_UNAVAILABLE_STRATEGY_INPUT_OUTPUT",
            }
            for result in evaluations
        )
        gross_known = all(trade.gross_pnl is not None for trade in trades)
        costed = gross_known and all(
            trade.charges is not None and trade.net_pnl is not None for trade in trades
        )
        row = await session.get(BacktestRun, manifest.run_id)
        row.status = (
            "FAILED"
            if worker.failed
            else "INCOMPLETE"
            if positions or pending or standdowns or strategy_errors or not costed
            else "COMPLETED"
        )
        row.finished_at = worker.clock.utcnow()
        row.error_detail = "WORKER_REVIEW_REQUIRED" if worker.failed else None
        samples = await stored_samples(session, manifest.run_id)
        links = copy_journal_trades(session, manifest.run_id, trades)
        metrics = summarise(samples, trades)
        session.add(
            BacktestResult(
                run_id=manifest.run_id,
                equity_curve=[[sample["observed_at"], sample["net_equity"]] for sample in samples],
                window_parameters={
                    "account_samples": samples,
                    "trade_links": links,
                    "descriptive_metrics": json_metrics(metrics),
                },
                total_return=metrics["total_return"],
                max_drawdown=metrics["max_drawdown"],
                win_rate=metrics["win_rate"],
                profit_factor=metrics["profit_factor"],
                expectancy=metrics["expectancy"],
                avg_holding_seconds=int(metrics["average_holding_seconds"])
                if metrics["average_holding_seconds"] is not None
                else None,
                drawdown_curve=[
                    [sample["observed_at"], str(drawdown) if drawdown is not None else None]
                    for sample, drawdown in zip(samples, metrics["drawdown"], strict=True)
                ],
                trade_count=len(trades),
                gross_pnl=sum((trade.gross_pnl for trade in trades), Decimal(0))
                if gross_known
                else None,
                total_charges=sum((trade.charges for trade in trades), Decimal(0))
                if costed
                else None,
                net_pnl=sum((trade.net_pnl for trade in trades), Decimal(0)) if costed else None,
                rejections=dict(Counter(reasons)),
                notes=(
                    f"REPLAY/SIMULATED; {positions} open positions; {pending} pending orders; "
                    f"{standdowns} source stand-downs. "
                    f"{strategy_errors} invalid strategy evaluations. "
                    "Timeline completion is not full-session coverage, "
                    "external validation or profitability evidence. "
                    "Descriptive metrics use sampled net equity; annualized metrics unavailable."
                ),
            )
        )
        await session.flush()
        catalog = await load_catalog(session, manifest.run_id)
        report_digest = catalog_digest(catalog)
        await AuditService(worker.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=f"history:{manifest.run_id}",
                event_type="HISTORICAL_RUN_FINISHED",
                actor="historical-runner",
                mode=TradingMode.PAPER,
            ),
            {
                "result": {
                    "status": row.status,
                    "open_positions": positions,
                    "pending_orders": pending,
                    "invalid_strategy_evaluations": strategy_errors,
                    "recording_sha256": manifest.recording_sha256,
                    "catalog_sha256": report_digest,
                    "reproducibility": fingerprints(catalog),
                }
            },
        )
        return row.status
