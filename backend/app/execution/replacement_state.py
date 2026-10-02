"""Replacement authorization and interruption recovery never create orders."""

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.modes import TradingMode


async def replacement_parent(session, proposal_id, chain):
    intents = list(
        await session.scalars(
            sa.select(AuditEvent).where(
                AuditEvent.proposal_id == proposal_id,
                AuditEvent.event_type == "ENTRY_REPLACE_INTENT",
                AuditEvent.mode == TradingMode.PAPER,
            )
        )
    )
    if not intents and chain is None:
        return None
    if len(intents) != 1 or intents[0].chain_id != chain:
        raise SafetyError("REPLACEMENT_AUTHORIZATION_REQUIRED")
    intent = intents[0]
    if not await AuditService().verify(chain, expected_count=1):
        raise SafetyError("REPLACEMENT_INTENT_NOT_OPEN")
    original = await session.get(Order, intent.order_id)
    if (
        original is None
        or original.mode != TradingMode.PAPER
        or original.role != "ENTRY"
        or original.status != OrderStatus.CANCELLED
        or original.filled_quantity != 0
    ):
        raise SafetyError("REPLACEMENT_REQUIRES_UNFILLED_CANCELLED_ENTRY")
    return original.id


async def finish_replacement(executor, intent, code, error=None):
    async with db_session.session_scope() as session:
        original = await session.get(Order, intent.order_id)
        replacement = await session.scalar(
            sa.select(Order).where(
                Order.proposal_id == intent.proposal_id,
                Order.role == "ENTRY",
            )
        )
        proposal = await session.get(Proposal, intent.proposal_id)
        if proposal is None:
            raise SafetyError("REPLACEMENT_PROPOSAL_UNAVAILABLE")
        if replacement is not None and replacement.parent_order_id != intent.order_id:
            raise SafetyError("REPLACEMENT_ORDER_LINEAGE_MISMATCH")
        if replacement is None:
            proposal.status = "EXECUTION_BLOCKED"
            proposal.rejection_code = "REPLACEMENT_NOT_SUBMITTED"
        result = {
            "original_order_id": intent.order_id,
            "original_status": original.status.value if original else "MISSING",
            "replacement_proposal_id": intent.proposal_id,
            "replacement_order_id": replacement.id if replacement else None,
            "replacement_status": replacement.status.value if replacement else "NOT_SUBMITTED",
            "code": code,
            "error": error,
        }
        await AuditService(executor.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=intent.chain_id,
                event_type="ENTRY_REPLACE_RESULT",
                actor="paper_execution",
                mode=TradingMode.PAPER,
            ),
            {"order_id": intent.order_id, "proposal_id": intent.proposal_id, "result": result},
            expected_count=1,
        )
    return {"audit_chain_id": intent.chain_id, **result}


async def recover_replacements(executor):
    result = sa.orm.aliased(AuditEvent)
    async with db_session.session_scope() as session:
        intents = list(
            await session.scalars(
                sa.select(AuditEvent).where(
                    AuditEvent.mode == TradingMode.PAPER,
                    AuditEvent.event_type == "ENTRY_REPLACE_INTENT",
                    ~sa.exists(
                        sa.select(result.id).where(
                            result.chain_id == AuditEvent.chain_id,
                            result.event_type == "ENTRY_REPLACE_RESULT",
                        )
                    ),
                )
            )
        )
    for intent in intents:
        if not await AuditService(executor.clock).verify(intent.chain_id, expected_count=1):
            raise SafetyError("REPLACEMENT_INTENT_INTEGRITY_FAILURE")
        await finish_replacement(executor, intent, "INTERRUPTED_REVIEW_REQUIRED")
