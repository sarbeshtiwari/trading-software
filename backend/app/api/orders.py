"""Authenticated PAPER entry-order controls; never cancel protective exits."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.execution.bulk_cancel import cancel_entries
from app.execution.owner_cancel import cancel_entry
from app.execution.replacement import replace_entry
from app.modes import TradingMode
from app.trading.worker import active_worker

router = APIRouter(prefix="/orders", tags=["orders"])


class CancelEntryRequest(EvidenceModel):
    request_id: UUID
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


class CancelEntryResult(EvidenceModel):
    order_id: str
    audit_chain_id: str
    reason: str
    status: str
    filled_quantity: int | None
    terminal: bool
    error: str | None


class BulkCancelResult(EvidenceModel):
    audit_chain_id: str
    outcomes: list[CancelEntryResult]
    excluded_exit_ids: list[str]
    resolved: bool


class ReplacementRequest(CancelEntryRequest):
    replacement_proposal_id: str = Field(min_length=1, max_length=40)


class ReplacementResult(EvidenceModel):
    audit_chain_id: str
    original_order_id: str
    original_status: str
    replacement_proposal_id: str
    replacement_order_id: str | None
    replacement_status: str
    code: str
    error: str | None


class ReplacementCandidate(EvidenceModel):
    proposal_id: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    approved_quantity: int
    created_at: datetime


@router.get("/{identifier}/replacement-proposals", response_model=list[ReplacementCandidate])
async def replacement_proposals(identifier: str):
    async with db_session.session_scope() as session:
        original = await session.get(Order, identifier)
        if original is None or original.mode != TradingMode.PAPER or original.role != "ENTRY":
            raise HTTPException(404, "PAPER entry unavailable")
        prior = await session.get(Proposal, original.proposal_id)
        if prior is None:
            raise HTTPException(409, "Original proposal unavailable")
        rows = list(
            await session.scalars(
                sa.select(Proposal)
                .where(
                    Proposal.mode == TradingMode.PAPER,
                    Proposal.status == "RISK_APPROVED",
                    Proposal.instrument_id == original.instrument_id,
                    Proposal.strategy_id == original.strategy_id,
                    Proposal.product == original.product,
                    Proposal.direction == prior.direction,
                    Proposal.id != prior.id,
                    Proposal.approved_quantity > 0,
                    ~sa.exists(sa.select(Order.id).where(Order.proposal_id == Proposal.id)),
                )
                .order_by(Proposal.created_at.desc())
                .limit(100)
            )
        )
    return [
        ReplacementCandidate(
            proposal_id=row.id,
            entry=row.entry_price,
            stop=row.stop_loss,
            target=row.target_price,
            approved_quantity=row.approved_quantity,
            created_at=_utc(row.created_at),
        )
        for row in rows
    ]


@router.post("/{identifier}/replace", response_model=ReplacementResult)
async def replace(identifier: str, body: ReplacementRequest, request: Request):
    actor = request.state.principal.username
    try:
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if body.confirmation != "REPLACE PAPER ENTRY":
            raise SafetyError("REPLACEMENT_CONFIRMATION_REQUIRED")
        return await replace_entry(
            active_worker(),
            identifier,
            body.replacement_proposal_id,
            actor=actor,
            request_id=body.request_id,
            reason=body.reason,
        )
    except SafetyError as error:
        await AuditService().append(
            AuditIdentity(
                chain_id=new_id("rep"),
                event_type="OWNER_REPLACE_REFUSED",
                actor=actor,
                mode=get_settings().trading_mode,
            ),
            {
                "order_id": identifier,
                "proposal_id": body.replacement_proposal_id,
                "result": {
                    "request_id": str(body.request_id),
                    "reason": body.reason,
                    "code": error.message,
                },
            },
        )
        raise HTTPException(409, error.message) from None


@router.post("/cancel-entries", response_model=BulkCancelResult)
async def cancel_all_entries(body: CancelEntryRequest, request: Request):
    actor = request.state.principal.username
    try:
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if body.confirmation != "CANCEL PAPER ENTRIES":
            raise SafetyError("BULK_CANCEL_CONFIRMATION_REQUIRED")
        return await cancel_entries(
            active_worker(), actor=actor, request_id=body.request_id, reason=body.reason
        )
    except SafetyError as error:
        await AuditService().append(
            AuditIdentity(
                chain_id=new_id("blk"),
                event_type="OWNER_BULK_CANCEL_REFUSED",
                actor=actor,
                mode=get_settings().trading_mode,
            ),
            {
                "result": {
                    "request_id": str(body.request_id),
                    "reason": body.reason,
                    "code": error.message,
                }
            },
        )
        raise HTTPException(409, error.message) from None


@router.post("/{identifier}/cancel", response_model=CancelEntryResult)
async def cancel(identifier: str, body: CancelEntryRequest, request: Request):
    actor = request.state.principal.username
    try:
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if body.confirmation != "CANCEL PAPER ENTRY":
            raise SafetyError("CANCEL_CONFIRMATION_REQUIRED")
        return await cancel_entry(
            active_worker(),
            identifier,
            actor=actor,
            request_id=body.request_id,
            reason=body.reason,
        )
    except SafetyError as error:
        await AuditService().append(
            AuditIdentity(
                chain_id=new_id("can"),
                event_type="OWNER_CANCEL_REFUSED",
                actor=actor,
                mode=get_settings().trading_mode,
            ),
            {
                "result": {
                    "order_id": identifier,
                    "request_id": str(body.request_id),
                    "reason": body.reason,
                    "code": error.message,
                }
            },
        )
        raise HTTPException(409, error.message) from None
