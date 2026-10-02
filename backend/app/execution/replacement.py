"""PAPER cancel/replace consumes fresh canonical approvals, never owner quantities."""

from hashlib import sha256
from uuid import UUID, uuid5

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import Proposal, RiskDecision
from app.db.models.trading import Order
from app.execution.freshness import require_entry_sources
from app.execution.hygiene import PaperOrderHygiene
from app.execution.owner_cancel import cancel_entry_locked
from app.execution.paper import approved_risk, require_enabled_strategy
from app.execution.replacement_state import finish_replacement
from app.modes import TradingMode
from app.risk.active import active_limits


async def validate_replacement(worker, identifier, proposal_id):
    async with db_session.session_scope() as session:
        original = await session.get(Order, identifier)
        proposal = await session.get(Proposal, proposal_id)
        risk = await session.scalar(
            sa.select(RiskDecision).where(
                RiskDecision.proposal_id == proposal_id,
                RiskDecision.is_preflight.is_(False),
            )
        )
        existing = await session.scalar(sa.select(Order.id).where(Order.proposal_id == proposal_id))
        limits = await active_limits(session)
    if (
        original is None
        or original.mode != TradingMode.PAPER
        or original.role != "ENTRY"
        or original.status not in {OrderStatus.OPEN, OrderStatus.PENDING}
        or original.filled_quantity
        or not original.broker_order_id
    ):
        raise SafetyError("UNFILLED_ACKNOWLEDGED_PAPER_ENTRY_REQUIRED")
    if (
        proposal is None
        or risk is None
        or limits is None
        or existing
        or proposal.id == original.proposal_id
        or proposal.mode != TradingMode.PAPER
        or proposal.status != "RISK_APPROVED"
        or proposal.instrument_id != original.instrument_id
        or proposal.strategy_id != original.strategy_id
        or proposal.product != original.product
    ):
        raise SafetyError("FRESH_MATCHING_REPLACEMENT_APPROVAL_REQUIRED")
    async with db_session.session_scope() as session:
        prior = await session.get(Proposal, original.proposal_id)
    if prior is None or proposal.direction != prior.direction:
        raise SafetyError("REPLACEMENT_DIRECTION_MISMATCH")
    await approved_risk(proposal, risk)
    await require_entry_sources(proposal, limits, worker.clock.utcnow())
    await require_enabled_strategy(proposal, worker.settings, clock=worker.clock)
    if identifier in await PaperOrderHygiene(worker.executor).unfinished():
        raise SafetyError("ORDER_CANCELLATION_ALREADY_PENDING")
    return original, proposal


async def prepare_replacement(worker, original, proposal, *, chain, actor, binding):
    child_id = str(uuid5(UUID(binding["request_id"]), "cancel-original"))
    child_chain = "can" + sha256(child_id.encode()).hexdigest()[:32]
    audit = AuditService(worker.clock)
    async with db_session.session_scope() as session:
        changed = await session.execute(
            sa.update(Proposal)
            .where(
                Proposal.id == proposal.id,
                Proposal.status == "RISK_APPROVED",
            )
            .values(status="REPLACEMENT_RESERVED")
        )
        if changed.rowcount != 1:
            raise SafetyError("REPLACEMENT_APPROVAL_ALREADY_CONSUMED")
        intent = await audit.append_in_session(
            session,
            AuditIdentity(
                chain_id=chain,
                event_type="ENTRY_REPLACE_INTENT",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {
                "order_id": original.id,
                "proposal_id": proposal.id,
                "result": {**binding, "cancellation_request_id": child_id},
            },
            expected_count=0,
        )
        await audit.append_in_session(
            session,
            AuditIdentity(
                chain_id=child_chain,
                event_type="ENTRY_CANCEL_INTENT",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {
                "order_id": original.id,
                "proposal_id": original.proposal_id,
                "instrument_id": original.instrument_id,
                "result": {
                    "reason": "OWNER_CANCEL",
                    "owner_reason": binding["reason"],
                    "request_id": child_id,
                    "replacement_chain_id": chain,
                    "status_before": original.status.value,
                },
            },
            expected_count=0,
        )
    return intent


async def replace_entry(worker, identifier, proposal_id, *, actor, request_id, reason):
    if worker is None:
        raise SafetyError("PAPER_WORKER_UNAVAILABLE")
    async with worker.cycle_lock:
        if not worker.running or worker.settings.trading_mode != TradingMode.PAPER:
            raise SafetyError("RUNNING_PAPER_WORKER_REQUIRED")
        audit = AuditService(worker.clock)
        chain = "rep" + sha256(str(request_id).encode()).hexdigest()[:32]
        binding = {
            "request_id": str(request_id),
            "reason": reason,
            "original_order_id": identifier,
            "replacement_proposal_id": proposal_id,
        }
        records = await audit.chain(chain)
        if records:
            if not await audit.verify(chain) or len(records) not in {1, 2}:
                raise SafetyError("REPLACEMENT_REQUEST_INTEGRITY_FAILURE")
            intent = records[0]
            if (
                intent.actor != actor
                or intent.event_type != "ENTRY_REPLACE_INTENT"
                or any(intent.result.get(key) != value for key, value in binding.items())
            ):
                raise SafetyError("REPLACEMENT_REQUEST_ID_CONFLICT")
            if len(records) == 2:
                if records[1].event_type != "ENTRY_REPLACE_RESULT":
                    raise SafetyError("REPLACEMENT_REQUEST_INTEGRITY_FAILURE")
                return {"audit_chain_id": chain, **records[1].result}
            return await finish_replacement(worker.executor, intent, "INTERRUPTED_REVIEW_REQUIRED")
        if worker.failed or not worker.executor.gate.new_entries_allowed:
            raise SafetyError("REPLACEMENT_ENTRIES_BLOCKED")
        original, proposal = await validate_replacement(worker, identifier, proposal_id)
        intent = await prepare_replacement(
            worker, original, proposal, chain=chain, actor=actor, binding=binding
        )
        try:
            cancellation = await cancel_entry_locked(
                worker,
                identifier,
                actor=actor,
                request_id=intent.result["cancellation_request_id"],
                reason=reason,
            )
            if (
                cancellation["status"] != "CANCELLED"
                or cancellation["filled_quantity"] != 0
                or cancellation["error"]
            ):
                raise SafetyError("CANCEL_NOT_SAFE_TO_REPLACE")
            if not await PaperOrderHygiene(worker.executor).run():
                raise SafetyError("CANCELLATION_RECONCILIATION_REQUIRED")
            async with db_session.session_scope() as session:
                changed = await session.execute(
                    sa.update(Proposal)
                    .where(
                        Proposal.id == proposal_id,
                        Proposal.status == "REPLACEMENT_RESERVED",
                    )
                    .values(status="RISK_APPROVED")
                )
                if changed.rowcount != 1:
                    raise SafetyError("REPLACEMENT_RESERVATION_CHANGED")
            await worker.executor.submit(proposal_id, replacement_chain=chain)
        except Exception as error:
            code = error.message if isinstance(error, SafetyError) else type(error).__name__
            return await finish_replacement(
                worker.executor, intent, "REPLACEMENT_NOT_CONFIRMED", code
            )
        return await finish_replacement(worker.executor, intent, "REPLACEMENT_SUBMISSION_RECORDED")
