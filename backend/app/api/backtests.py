"""Authenticated historical inspection of the configured database only."""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy.orm import noload

from app.analysis.equity import EvidenceModel
from app.api.workspace import Row
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical
from app.backtest.adequacy import WindowDisclosure
from app.backtest.catalog import catalog_digest, load_catalog
from app.backtest.universe import UNIVERSE_WARNING, UniverseDisclosure
from app.backtest.wf_integrity import report_integrity
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.modes import TradingMode
from app.strategies.approval import EvidenceReview, OOSValidationPolicy, evaluate_oos
from app.strategies.evidence import BindingSummary, StrategyBinding

router = APIRouter(prefix="/backtests", tags=["backtests"])


class RunView(Row):
    id: str
    kind: str
    strategy_id: str
    strategy_version: str
    status: str
    progress_pct: Decimal
    simulated: Literal[True]
    initial_capital: Decimal
    start_date: date
    end_date: date
    started_at: datetime | None
    finished_at: datetime | None
    data_window_warning: str | None
    survivorship_note: str | None


class RunsView(Row):
    simulated: Literal[True] = True
    runs: list[RunView]
    has_more: bool
    scope: str = "Configured database only; isolated runs are not automatically published here."


class OOSDiagnostics(Row):
    account_model: Literal["INDEPENDENT_WINDOW_ACCOUNTS"]
    source_run_id: str | None = None
    selected_candidate: str | None = None
    strategy_risk_fraction: Decimal | None = None
    training_net_return: Decimal | None = None
    oos_net_return: Decimal | None = None
    net_return_drop: Decimal | None = None
    degradation_threshold: Decimal | None = None
    degradation_state: Literal["UNAVAILABLE", "FLAGGED", "WITHIN_THRESHOLD"]
    window_count: int | None = None
    candidate_choices: list[str] | None = None
    risk_fraction_choices: list[Decimal] | None = None
    parameter_changes: int | None = None
    absolute_risk_fraction_drift: Decimal | None = None
    profitable_window_fraction: Decimal | None = None
    max_within_window_drawdown: Decimal | None = None
    strategy_binding: StrategyBinding | None = None
    binding_summary: BindingSummary | None = None


class ResultView(Row):
    simulated: Literal[True] = True
    id: str
    window_kind: str
    window_index: int | None
    trade_count: int
    gross_pnl: Decimal | None
    total_charges: Decimal | None
    net_pnl: Decimal | None
    total_return: Decimal | None
    max_drawdown: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    avg_holding_seconds: int | None
    sharpe: Decimal | None
    sortino: Decimal | None
    cagr: Decimal | None
    notes: str | None
    overfitting_flag: bool | None
    diagnostics: OOSDiagnostics | None = None


class ReproducibilityView(Row):
    version: Literal[1]
    inputs_sha256: str
    outcomes_sha256: str
    metrics_sha256: str
    trades_sha256: str
    simulated: Literal[True]


class RunDetail(Row):
    window_disclosure: WindowDisclosure | None = None
    universe_warning: str = UNIVERSE_WARNING
    simulated: Literal[True] = True
    universe_disclosure: UniverseDisclosure | None = None
    run: RunView
    results: list[ResultView]
    recording_sha256: str | None
    fill_convention: str | None
    launch_available: bool = False
    reproducibility: ReproducibilityView | None = None
    catalog_integrity: str = "NOT_EVALUATED"
    evidence_review: EvidenceReview | None = None
    strategy_binding: StrategyBinding | None = None


class ReviewRequest(EvidenceModel):
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["REVIEW OOS EVIDENCE"]


def review_report(row, results, integrity):
    raw = (row.parameters or {}).get("validation")
    policy = OOSValidationPolicy.model_validate(raw) if raw is not None else None
    aggregate = next((item for item in results if item.window_kind == "OOS_AGGREGATE"), None)
    return evaluate_oos(policy, aggregate, integrity=integrity)


class TradeView(Row):
    simulated: Literal[True] = True
    id: str
    window_index: int | None
    source_run_id: str | None = None
    source_trade_id: str | None = None
    trading_symbol: str
    direction: str
    quantity: int
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime | None
    exit_price: Decimal | None
    gross_pnl: Decimal | None
    charges: Decimal | None
    net_pnl: Decimal | None


class TradesView(Row):
    simulated: Literal[True] = True
    trades: list[TradeView]
    has_more: bool


class SampleView(Row):
    simulated: Literal[True] = True
    audit_event_id: str
    observed_at: datetime
    baseline: bool
    status: str
    reasons: list[str]
    net_equity: Decimal | None
    gross_exposure: Decimal | None
    charges: Decimal | None
    drawdown: Decimal | None = None


class SamplesView(Row):
    simulated: Literal[True] = True
    samples: list[SampleView]
    has_more: bool


class ExportResult(ResultView):
    equity_curve: list[tuple[datetime, Decimal | None]] | None
    drawdown_curve: list[tuple[datetime, Decimal | None]] | None


class ReportExport(Row):
    window_disclosure: WindowDisclosure | None = None
    universe_warning: str = UNIVERSE_WARNING
    version: Literal[1] = 1
    simulated: Literal[True] = True
    scope: str = (
        "Sealed research report, not a full trading/audit database backup or LIVE evidence."
    )
    catalog_integrity: Literal["AUDIT_BOUND"] = "AUDIT_BOUND"
    catalog_sha256: str
    run: RunView
    universe_disclosure: UniverseDisclosure | None
    results: list[ExportResult]
    trades: list[TradeView]


MAX_EXPORT_ROWS = 10000
MAX_EXPORT_BYTES = 16 * 1024 * 1024


@router.get("/{run_id}/export", response_model=ReportExport)
async def export_report(run_id: str, request: Request):
    async with db_session.session_scope() as session:
        if await session.scalar(sa.select(BacktestRun.id).where(BacktestRun.id == run_id)) is None:
            raise HTTPException(404, "Historical run not found")
        for model in (BacktestResult, BacktestTrade):
            count = await session.scalar(
                sa.select(sa.func.count()).select_from(model).where(model.run_id == run_id)
            )
            if count > MAX_EXPORT_ROWS:
                raise HTTPException(413, "Historical export exceeds row limit; no partial export")
        catalog = await load_catalog(session, run_id)
        row = SimpleNamespace(**catalog["run"][0])
        if await report_integrity(session, row, catalog=catalog) != "AUDIT_BOUND":
            raise HTTPException(409, "Audit-bound simulated report required for export")
        links = {
            link["report_trade_id"]: link
            for result in catalog["results"]
            for link in (result["window_parameters"] or {})
            .get("diagnostics", {})
            .get("trade_links", [])
        }
        exported = ReportExport(
            window_disclosure=row.assumptions.get("window_disclosure"),
            catalog_sha256=catalog_digest(catalog),
            run=RunView.model_validate(row),
            universe_disclosure=row.assumptions.get("universe_disclosure"),
            results=[
                ExportResult.model_validate(
                    result | {"diagnostics": (result["window_parameters"] or {}).get("diagnostics")}
                )
                for result in catalog["results"]
            ],
            trades=[
                TradeView.model_validate(trade | links.get(trade["id"], {}))
                for trade in catalog["trades"]
            ],
        )
        encoded = canonical(exported.model_dump(mode="json")).encode("utf-8")
        if len(encoded) > MAX_EXPORT_BYTES:
            raise HTTPException(413, "Historical export exceeds 16 MiB; no partial export")
        await AuditService().append_in_session(
            session,
            AuditIdentity(
                chain_id=new_id("bex"),
                event_type="HISTORICAL_REPORT_EXPORTED",
                actor=request.state.principal.username,
                mode=TradingMode.PAPER,
            ),
            {
                "result": {
                    "run_id": run_id,
                    "simulated": True,
                    "catalog_sha256": exported.catalog_sha256,
                    "export_sha256": hashlib.sha256(encoded).hexdigest(),
                    "trade_count": len(exported.trades),
                    "result_count": len(exported.results),
                }
            },
        )
        return Response(
            encoded,
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="simulated-backtest.json"',
                "Cache-Control": "no-store",
            },
        )


async def require_run(session, run_id):
    row = await session.scalar(
        sa.select(BacktestRun)
        .options(noload(BacktestRun.results), noload(BacktestRun.trades))
        .where(BacktestRun.id == run_id)
    )
    if row is None:
        raise HTTPException(404, "Historical run not found")
    integrity = await report_integrity(session, row)
    if integrity == "INVALID":
        raise HTTPException(409, "Historical report integrity invalid; inspect audit evidence")
    return row, integrity


@router.get("", response_model=RunsView)
async def list_runs(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    sa.select(BacktestRun)
                    .options(noload(BacktestRun.results), noload(BacktestRun.trades))
                    .order_by(BacktestRun.created_at.desc(), BacktestRun.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
        if any(not row.simulated for row in rows[:limit]):
            raise HTTPException(409, "Historical simulation label invalid; inspect audit evidence")
        return RunsView(
            runs=[RunView.model_validate(row) for row in rows[:limit]], has_more=len(rows) > limit
        )


@router.get("/{run_id}", response_model=RunDetail)
async def run_detail(run_id: str):
    async with db_session.session_scope() as session:
        row, integrity = await require_run(session, run_id)
        results = list(
            (
                await session.scalars(
                    sa.select(BacktestResult)
                    .where(BacktestResult.run_id == run_id)
                    .order_by(BacktestResult.id)
                )
            ).all()
        )
        finished = await session.scalar(
            sa.select(AuditEvent)
            .where(
                AuditEvent.chain_id == f"history:{run_id}",
                AuditEvent.event_type == "HISTORICAL_RUN_FINISHED",
            )
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        return RunDetail(
            window_disclosure=row.assumptions.get("window_disclosure"),
            universe_disclosure=row.assumptions.get("universe_disclosure"),
            run=RunView.model_validate(row),
            results=[
                ResultView.model_validate(result).model_copy(
                    update={
                        "diagnostics": OOSDiagnostics.model_validate(
                            result.window_parameters["diagnostics"]
                        )
                        if result.window_parameters and result.window_parameters.get("diagnostics")
                        else None,
                    }
                )
                for result in results
            ],
            recording_sha256=row.assumptions.get("recording_sha256"),
            fill_convention=row.assumptions.get("fill_convention"),
            reproducibility=finished.result.get("reproducibility") if finished else None,
            catalog_integrity=integrity,
            strategy_binding=StrategyBinding.model_validate(row.assumptions["strategy_binding"])
            if row.assumptions.get("strategy_binding")
            else None,
            evidence_review=review_report(row, results, integrity)
            if row.kind == "WALKFORWARD"
            else None,
        )


@router.post("/{run_id}/review", response_model=EvidenceReview)
async def review_evidence(run_id: str, body: ReviewRequest, request: Request):
    async with db_session.session_scope() as session:
        row, integrity = await require_run(session, run_id)
        if row.kind != "WALKFORWARD" or integrity != "AUDIT_BOUND":
            raise HTTPException(409, "Completed audit-bound walk-forward report required")
        results = list(
            (
                await session.scalars(
                    sa.select(BacktestResult).where(BacktestResult.run_id == run_id)
                )
            ).all()
        )
        review = review_report(row, results, integrity)
        event = await AuditService().append_in_session(
            session,
            AuditIdentity(
                chain_id=f"validation:{run_id}",
                event_type="OOS_EVIDENCE_REVIEWED",
                actor=request.state.principal.username,
                mode=TradingMode.PAPER,
            ),
            {
                "result": {
                    "run_id": run_id,
                    "reason": body.reason,
                    "catalog_sha256": catalog_digest(await load_catalog(session, run_id)),
                    "review": review.model_dump(mode="json"),
                }
            },
        )
        return review.model_copy(update={"audit_event_id": event.id})


@router.get("/{run_id}/trades", response_model=TradesView)
async def run_trades(
    run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
):
    async with db_session.session_scope() as session:
        await require_run(session, run_id)
        rows = list(
            (
                await session.scalars(
                    sa.select(BacktestTrade)
                    .where(BacktestTrade.run_id == run_id)
                    .order_by(BacktestTrade.entry_ts, BacktestTrade.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
        windows = list(
            (
                await session.scalars(
                    sa.select(BacktestResult).where(
                        BacktestResult.run_id == run_id,
                        BacktestResult.window_kind == "OUT_OF_SAMPLE",
                    )
                )
            ).all()
        )
        links = {
            link["report_trade_id"]: link
            for window in windows
            for link in (window.window_parameters or {})
            .get("diagnostics", {})
            .get("trade_links", [])
        }
        return TradesView(
            trades=[
                TradeView.model_validate(
                    TradeView.model_validate(row).model_dump() | links.get(row.id, {})
                )
                for row in rows[:limit]
            ],
            has_more=len(rows) > limit,
        )


@router.get("/{run_id}/samples", response_model=SamplesView)
async def run_samples(
    run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
):
    async with db_session.session_scope() as session:
        await require_run(session, run_id)
        result = await session.scalar(
            sa.select(BacktestResult).where(
                BacktestResult.run_id == run_id, BacktestResult.window_kind == "FULL"
            )
        )
        drawdowns = {}
        if result and result.window_parameters and result.drawdown_curve is not None:
            samples = result.window_parameters.get("account_samples", [])
            if len(samples) == len(result.drawdown_curve):
                drawdowns = {
                    sample["audit_event_id"]: point[1]
                    for sample, point in zip(samples, result.drawdown_curve, strict=True)
                }
        rows = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.chain_id == f"history:{run_id}",
                        AuditEvent.event_type == "HISTORICAL_ACCOUNT_SAMPLE",
                    )
                    .order_by(AuditEvent.sequence)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
        return SamplesView(
            samples=[
                SampleView.model_validate(
                    row.result | {"audit_event_id": row.id, "drawdown": drawdowns.get(row.id)}
                )
                for row in rows[:limit]
            ],
            has_more=len(rows) > limit,
        )
