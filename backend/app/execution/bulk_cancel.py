"""Atomic bulk-entry intent; supervisor recovery owns every unfinished child."""

from hashlib import sha256
from uuid import UUID, uuid5

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.trading import Order
from app.execution.hygiene import PaperOrderHygiene
from app.execution.owner_cancel import cancel_entry_locked
from app.modes import TradingMode


async def prepare(audit, worker, chain, *, actor, request_id, reason):
    pending = await PaperOrderHygiene(worker.executor).unfinished()
    async with db_session.session_scope() as session:
        orders = list(
            await session.scalars(
                sa.select(Order)
                .where(
                    Order.mode == TradingMode.PAPER,
                    ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
                )
                .order_by(Order.id)
            )
        )
        entries = [order for order in orders if order.role == "ENTRY"]
        if any(order.id in pending for order in entries):
            raise SafetyError("EXISTING_CANCELLATION_REQUIRES_RECOVERY")
        items = [
            {"order_id": order.id, "request_id": str(uuid5(UUID(str(request_id)), order.id))}
            for order in entries
        ]
        plan = {
            "request_id": str(request_id),
            "owner_reason": reason,
            "items": items,
            "excluded_exit_ids": [order.id for order in orders if order.role != "ENTRY"],
        }
        await audit.append_in_session(
            session,
            AuditIdentity(
                chain_id=chain,
                actor=actor,
                mode=TradingMode.PAPER,
                event_type="BULK_ENTRY_CANCEL_INTENT",
            ),
            {"result": plan},
            expected_count=0,
        )
        for order, item in zip(entries, items, strict=True):
            child = "can" + sha256(item["request_id"].encode()).hexdigest()[:32]
            await audit.append_in_session(
                session,
                AuditIdentity(
                    chain_id=child,
                    actor=actor,
                    mode=TradingMode.PAPER,
                    event_type="ENTRY_CANCEL_INTENT",
                ),
                {
                    "order_id": order.id,
                    "proposal_id": order.proposal_id,
                    "instrument_id": order.instrument_id,
                    "result": {
                        "reason": "OWNER_CANCEL",
                        "owner_reason": reason,
                        "request_id": item["request_id"],
                        "bulk_chain_id": chain,
                        "status_before": order.status.value,
                    },
                },
                expected_count=0,
            )
    return plan


async def cancel_entries(worker, *, actor, request_id, reason):
    if worker is None:
        raise SafetyError("PAPER_WORKER_UNAVAILABLE")
    async with worker.cycle_lock:
        if not worker.running or worker.settings.trading_mode != TradingMode.PAPER:
            raise SafetyError("RUNNING_PAPER_WORKER_REQUIRED")
        audit = AuditService(worker.clock)
        chain = "blk" + sha256(str(request_id).encode()).hexdigest()[:32]
        records = await audit.chain(chain)
        if records:
            if not await audit.verify(chain) or len(records) not in {1, 2}:
                raise SafetyError("BULK_CANCEL_INTEGRITY_FAILURE")
            intent = records[0]
            if (
                intent.event_type != "BULK_ENTRY_CANCEL_INTENT"
                or intent.actor != actor
                or intent.result.get("request_id") != str(request_id)
                or intent.result.get("owner_reason") != reason
            ):
                raise SafetyError("BULK_CANCEL_REQUEST_ID_CONFLICT")
            if len(records) == 2:
                if records[1].event_type != "BULK_ENTRY_CANCEL_RESULT":
                    raise SafetyError("BULK_CANCEL_INTEGRITY_FAILURE")
                return {"audit_chain_id": chain, **records[1].result}
            plan = intent.result
        else:
            worker.executor.gate.block("paper_order_hygiene", "PAPER_BULK_CANCELLATION_PENDING")
            plan = await prepare(
                audit, worker, chain, actor=actor, request_id=request_id, reason=reason
            )
        outcomes = []
        for item in plan["items"]:
            outcomes.append(
                await cancel_entry_locked(
                    worker,
                    item["order_id"],
                    actor=actor,
                    request_id=item["request_id"],
                    reason=reason,
                )
            )
        result = {
            "outcomes": outcomes,
            "excluded_exit_ids": plan["excluded_exit_ids"],
            "resolved": all(outcome["terminal"] and not outcome["error"] for outcome in outcomes),
        }
        await audit.append(
            AuditIdentity(
                chain_id=chain,
                actor=actor,
                mode=TradingMode.PAPER,
                event_type="BULK_ENTRY_CANCEL_RESULT",
            ),
            {"result": result},
        )
        return {"audit_chain_id": chain, **result}
