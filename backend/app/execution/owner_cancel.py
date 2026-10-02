"""Owner cancellation shares durable intent recovery with the PAPER supervisor."""

from hashlib import sha256

from app.agents.validation import _utc
from app.audit.service import AuditService
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.trading import Order
from app.execution.hygiene import PaperOrderHygiene
from app.modes import TradingMode


async def previous_request(audit, chain, identifier, actor, binding):
    records = await audit.chain(chain)
    if not records:
        return records
    if not await audit.verify(chain) or len(records) not in {1, 2}:
        raise SafetyError("CANCEL_REQUEST_INTEGRITY_FAILURE")
    intent = records[0]
    if (
        intent.event_type != "ENTRY_CANCEL_INTENT"
        or intent.actor != actor
        or intent.order_id != identifier
        or any(intent.result.get(key) != value for key, value in binding.items())
    ):
        raise SafetyError("CANCEL_REQUEST_ID_CONFLICT")
    if len(records) == 2 and records[1].event_type != "ENTRY_CANCEL_RESULT":
        raise SafetyError("CANCEL_REQUEST_INTEGRITY_FAILURE")
    return records


async def cancel_entry(worker, identifier, *, actor, request_id, reason):
    if worker is None:
        raise SafetyError("PAPER_WORKER_UNAVAILABLE")
    async with worker.cycle_lock:
        if not worker.running or worker.settings.trading_mode != TradingMode.PAPER:
            raise SafetyError("RUNNING_PAPER_WORKER_REQUIRED")
        executor = worker.executor
        audit = AuditService(worker.clock)
        chain = "can" + sha256(str(request_id).encode()).hexdigest()[:32]
        binding = {"reason": "OWNER_CANCEL", "owner_reason": reason, "request_id": str(request_id)}
        records = await previous_request(audit, chain, identifier, actor, binding)
        if len(records) == 2:
            return {"order_id": identifier, "audit_chain_id": chain, **records[1].result}
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
        if order is None or order.mode != TradingMode.PAPER or order.role != "ENTRY":
            raise SafetyError("PAPER_ENTRY_ORDER_REQUIRED")
        if order.status.is_terminal and not records:
            raise SafetyError("ORDER_ALREADY_TERMINAL")
        hygiene = PaperOrderHygiene(executor)
        pending = (await hygiene.unfinished()).get(identifier)
        if pending is not None and pending.chain_id != chain:
            raise SafetyError("ORDER_CANCELLATION_ALREADY_PENDING")
        executor.gate.block("paper_order_hygiene", "PAPER_ORDER_CANCELLATION_PENDING")
        if not records:
            await hygiene.record(
                order,
                chain,
                "ENTRY_CANCEL_INTENT",
                {**binding, "status_before": order.status.value},
                actor=actor,
            )
        error_code = None
        try:
            if _utc(order.submitted_at or order.created_at) > worker.clock.utcnow():
                raise SafetyError("FUTURE_ORDER_TIMESTAMP")
            await executor.cancel(identifier)
            await executor.verify_protection()
        except Exception as error:
            error_code = error.message if isinstance(error, SafetyError) else type(error).__name__
        async with db_session.session_scope() as session:
            current = await session.get(Order, identifier)
        result = {
            "reason": "OWNER_CANCEL",
            "status": current.status.value if current else "MISSING",
            "filled_quantity": current.filled_quantity if current else None,
            "terminal": current is not None and current.status.is_terminal,
            "error": error_code,
        }
        await hygiene.record(order, chain, "ENTRY_CANCEL_RESULT", result, actor=actor)
        return {"order_id": identifier, "audit_chain_id": chain, **result}
