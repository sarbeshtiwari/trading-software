"""Authenticated PAPER entry-order controls; never cancel protective exits."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.execution.owner_cancel import cancel_entry
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
