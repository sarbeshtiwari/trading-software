"""Owner-authenticated historical jobs with server-owned inputs only."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.api.workspace import Row
from app.backtest.cancellation import cancel_owned
from app.db import session as db_session
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob

router = APIRouter(prefix="/historical-jobs", tags=["historical-jobs"])


class PlanView(EvidenceModel):
    simulated: Literal[True] = True
    id: str
    label: str


class LaunchRequest(EvidenceModel):
    plan_id: str = Field(pattern=r"^[a-z0-9]{8,26}$")
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["RUN HISTORICAL PAPER"]


class LaunchResponse(EvidenceModel):
    simulated: Literal[True] = True
    id: str


class ExperimentRequest(LaunchRequest):
    confirmation: Literal["RUN WALK FORWARD"]


class JobView(Row):
    simulated: Literal[True] = True
    id: str
    status: str
    progress_pct: Decimal
    published: bool
    error_code: str | None
    updated_at: datetime
    liveness: str
    cancellable: bool


class CancelRequest(EvidenceModel):
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["CANCEL HISTORICAL RESEARCH"]


class CancelResponse(LaunchResponse):
    status: str


@router.get("/plans", response_model=list[PlanView])
async def plans(request: Request):
    try:
        return [
            PlanView(id=plan.id, label=plan.label)
            for plan in request.app.state.historical_jobs.plans()
        ]
    except Exception:
        raise HTTPException(503, "Historical plan configuration unavailable") from None


@router.post("", response_model=LaunchResponse, status_code=202)
async def launch(body: LaunchRequest, request: Request):
    try:
        identifier = await request.app.state.historical_jobs.enqueue(
            body.plan_id, actor=request.state.principal.username, reason=body.reason
        )
    except sa.exc.IntegrityError:
        raise HTTPException(409, "Historical job slot reserved; inspect existing jobs") from None
    except Exception:
        raise HTTPException(
            409, "Historical job unavailable; inspect owner configuration"
        ) from None
    return LaunchResponse(id=identifier)


@router.get("", response_model=list[JobView])
async def jobs(request: Request):
    manager = request.app.state.historical_jobs
    async with db_session.session_scope() as session:
        rows = (
            await session.scalars(
                sa.select(HistoricalJob).order_by(HistoricalJob.updated_at.desc()).limit(200)
            )
        ).all()
        return [
            JobView(
                id=row.id,
                status=row.status,
                progress_pct=row.progress_pct,
                published=row.published,
                error_code=row.error_code,
                updated_at=row.updated_at,
                cancellable=(
                    row.slot is not None
                    and row.owner_id == manager.owner_id
                    and row.id in manager.tasks
                    and not row.actor.startswith("walkforward-")
                ),
                liveness=(
                    "CONFIRMED_LOCAL"
                    if row.owner_id == manager.owner_id and row.id in manager.tasks
                    else "UNVERIFIED_OWNER_REVIEW_REQUIRED"
                    if row.slot
                    else "TERMINAL"
                ),
            )
            for row in rows
        ]


@router.get("/experiments/plans", response_model=list[PlanView])
async def experiments(request: Request):
    try:
        return [
            PlanView(id=plan.id, label=plan.label)
            for plan in request.app.state.walkforward_jobs.plans()
        ]
    except Exception:
        raise HTTPException(503, "Walk-forward configuration unavailable") from None


@router.post("/experiments", response_model=LaunchResponse, status_code=202)
async def launch_experiment(body: ExperimentRequest, request: Request):
    try:
        identifier = await request.app.state.walkforward_jobs.enqueue(
            body.plan_id, actor=request.state.principal.username, reason=body.reason
        )
    except Exception:
        raise HTTPException(
            409, "Walk-forward unavailable; inspect reservations and owner configuration"
        ) from None
    return LaunchResponse(id=identifier)


@router.get("/experiments", response_model=list[JobView])
async def experiment_jobs(request: Request):
    manager = request.app.state.walkforward_jobs
    async with db_session.session_scope() as session:
        rows = (
            await session.scalars(
                sa.select(WalkForwardJob).order_by(WalkForwardJob.updated_at.desc()).limit(200)
            )
        ).all()
        return [
            JobView(
                id=row.id,
                status=row.status,
                progress_pct=row.progress_pct,
                published=row.status == "COMPLETED",
                error_code=row.error_code,
                updated_at=row.updated_at,
                cancellable=(
                    row.slot is not None
                    and row.owner_id == manager.owner_id
                    and row.id in manager.tasks
                ),
                liveness=(
                    "CONFIRMED_LOCAL"
                    if row.owner_id == manager.owner_id and row.id in manager.tasks
                    else "UNVERIFIED_OWNER_REVIEW_REQUIRED"
                    if row.slot
                    else "TERMINAL"
                ),
            )
            for row in rows
        ]


async def _cancel(manager, identifier, model, prefix, *, body, request):
    try:
        status = await cancel_owned(
            manager,
            identifier,
            model,
            prefix,
            actor=request.state.principal.username,
            reason=body.reason,
        )
    except Exception:
        raise HTTPException(
            409, "Cancellation unconfirmed; inspect job ownership and state"
        ) from None
    return CancelResponse(id=identifier, status=status)


@router.post("/{identifier}/cancel", response_model=CancelResponse)
async def cancel_job(identifier: str, body: CancelRequest, request: Request):
    return await _cancel(
        request.app.state.historical_jobs,
        identifier,
        HistoricalJob,
        "hjob",
        body=body,
        request=request,
    )


@router.post("/experiments/{identifier}/cancel", response_model=CancelResponse)
async def cancel_experiment(identifier: str, body: CancelRequest, request: Request):
    return await _cancel(
        request.app.state.walkforward_jobs,
        identifier,
        WalkForwardJob,
        "wf",
        body=body,
        request=request,
    )
