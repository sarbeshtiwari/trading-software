"""Authenticated typed-confirmation PAPER emergency controls."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.config import get_settings
from app.core.errors import SafetyError
from app.emergency.audit import record_refusal
from app.emergency.controls import EmergencyControls
from app.emergency.flatten import flatten
from app.modes import TradingMode
from app.trading.recover import recover_worker
from app.trading.review import review_worker
from app.trading.worker import active_worker

router = APIRouter(prefix="/emergency", tags=["emergency"])


class EmergencyAction(EvidenceModel):
    action: Literal[
        "KILL", "DISABLE_ENTRIES", "FLATTEN", "CLEAR", "REVIEW_WORKER", "RECOVER_WORKER"
    ]
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


class EmergencyResult(EvidenceModel):
    entries_blocked: bool
    kill_switch: bool
    execution: str
    outcomes: list[dict[str, str]]
    review: dict[str, list[str]] | None = None


@router.post("", response_model=EmergencyResult)
async def activate(body: EmergencyAction, request: Request):
    actor = request.state.principal.username
    try:
        return await execute(body, actor)
    except (HTTPException, SafetyError) as error:
        await record_refusal(
            actor=actor,
            action=body.action,
            reason=body.reason,
            outcome="REQUEST_NOT_COMPLETED",
            code=error.detail if isinstance(error, HTTPException) else error.message,
        )
        raise
    except Exception as error:
        await record_refusal(
            actor=actor,
            action=body.action,
            reason=body.reason,
            outcome="FAILED_REVIEW_REQUIRED",
            code=type(error).__name__,
        )
        raise


async def execute(body: EmergencyAction, actor: str):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise HTTPException(423, "Only PAPER controls are implemented")
    phrases = {
        "RECOVER_WORKER": "RECOVER PAPER WORKER",
        "REVIEW_WORKER": "REVIEW PAPER WORKER",
        "CLEAR": "CLEAR PAPER EMERGENCY",
        "KILL": "KILL PAPER",
        "DISABLE_ENTRIES": "DISABLE PAPER ENTRIES",
        "FLATTEN": "FLATTEN PAPER",
    }
    if body.confirmation != phrases[body.action]:
        raise HTTPException(422, "Typed emergency confirmation does not match")
    if body.action == "RECOVER_WORKER":
        await asyncio.wait_for(
            recover_worker(active_worker(), actor=actor, reason=body.reason), timeout=20
        )
        state = await EmergencyControls().restore()
        return EmergencyResult(
            **state, execution="WORKER_RECOVERED_ENTRIES_REMAIN_DISABLED", outcomes=[]
        )
    if body.action == "REVIEW_WORKER":
        reviewed = await review_worker(active_worker(), actor=actor, reason=body.reason)
        state = await EmergencyControls().restore()
        return EmergencyResult(
            **state, execution="WORKER_REVIEWED_OTHER_GATES_REMAIN", outcomes=[], review=reviewed
        )
    if body.action == "CLEAR":
        state = await EmergencyControls().clear(active_worker(), actor=actor, reason=body.reason)
        return EmergencyResult(
            **state, execution="EMERGENCY_CLEARED_OTHER_GATES_REMAIN", outcomes=[]
        )
    state = await EmergencyControls().activate(body.action, actor=actor, reason=body.reason)
    outcomes = []
    execution = "ENTRIES_BLOCKED_EXITS_PERMITTED"
    if body.action == "FLATTEN":
        worker = active_worker()
        if worker is None:
            execution = "WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED"
            await record_refusal(
                actor=actor,
                action=body.action,
                reason=body.reason,
                outcome="ENTRIES_BLOCKED_FLATTEN_NOT_EXECUTED",
                code=execution,
            )
        else:
            outcomes = await flatten(worker, actor=actor, reason=body.reason)
            execution = "INSPECT_ORDER_AND_POSITION_OUTCOMES"
    return EmergencyResult(**state, execution=execution, outcomes=outcomes)


@router.get("", response_model=EmergencyResult)
async def current():
    state = await EmergencyControls().restore()
    return EmergencyResult(**state, execution="STATE_ONLY", outcomes=[])
