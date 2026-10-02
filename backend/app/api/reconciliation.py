"""Authenticated PAPER discrepancy inspection and explicitly reviewed resolution."""

import asyncio
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, Field

from app.analysis.equity import EvidenceModel
from app.config import get_settings
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.system import Discrepancy
from app.execution import discrepancies
from app.modes import TradingMode
from app.trading.worker import active_worker

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


class DiscrepancyRecord(EvidenceModel):
    id: str
    kind: str
    local_state: dict[str, Any] | None
    broker_state: dict[str, Any] | None
    delta: dict[str, Any] | None
    detail: str | None
    resolved: bool
    resolution: str | None
    resolved_by: str | None
    resolved_at: AwareDatetime | None
    detected_at: AwareDatetime


class DiscrepancyView(EvidenceModel):
    record: DiscrepancyRecord
    head_hash: str


class DiscrepancyPage(EvidenceModel):
    items: tuple[DiscrepancyView, ...]
    has_more: bool
    scope: str = (
        "PAPER evidence only; resolution records review, never repairs economics or arms trading."
    )


class ResolveRequest(EvidenceModel):
    expected_head: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


def require_paper():
    if get_settings().trading_mode != TradingMode.PAPER:
        raise HTTPException(423, "PAPER reconciliation only")


@router.get("", response_model=DiscrepancyPage)
async def listing(offset: int = Query(default=0, ge=0), resolved: bool = False):
    require_paper()
    try:
        async with db_session.session_scope() as session:
            rows = list(await session.scalars(sa.select(Discrepancy).where(
                Discrepancy.kind == discrepancies.KIND, Discrepancy.resolved == resolved
            ).order_by(Discrepancy.detected_at.desc(), Discrepancy.id.desc())
                .offset(offset).limit(51)))
            items = []
            for row in rows[:50]:
                history = await discrepancies.verify(session, row)
                items.append(DiscrepancyView(record=discrepancies.snapshot(row),
                                             head_hash=history[-1].record_hash))
        return DiscrepancyPage(items=tuple(items), has_more=len(rows) > 50)
    except (SafetyError, ValueError):
        raise HTTPException(409, "Discrepancy evidence unavailable or invalid") from None
    except Exception:
        raise HTTPException(503, "Discrepancy storage unavailable") from None


@router.post("/{identifier}/resolve", response_model=DiscrepancyView)
async def resolve(identifier: str, body: ResolveRequest, request: Request):
    require_paper()
    if body.confirmation != "RESOLVE PAPER DISCREPANCY":
        raise HTTPException(422, "Typed confirmation does not match")
    try:
        await asyncio.wait_for(discrepancies.resolve(
            active_worker(), identifier, actor=request.state.principal.username,
            reason=body.reason, expected_head=body.expected_head,
        ), timeout=20)
        async with db_session.session_scope() as session:
            row = await session.get(Discrepancy, identifier)
            records = await discrepancies.verify(session, row)
            return DiscrepancyView(
                record=discrepancies.snapshot(row), head_hash=records[-1].record_hash
            )
    except SafetyError as error:
        raise HTTPException(409, error.message) from None
    except Exception:
        raise HTTPException(
            503, "Discrepancy resolution unavailable; review current state"
        ) from None
