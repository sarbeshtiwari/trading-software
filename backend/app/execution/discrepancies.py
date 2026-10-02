"""Durable PAPER discrepancy evidence; resolution never repairs account economics."""

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.system import SINGLETON_ID, Discrepancy, SystemState
from app.modes import TradingMode

KIND = "PAPER_RECONCILIATION"
MAX_AUDIT_RECORDS = 1000


def order_snapshot(order):
    if order is None:
        return None
    fields = (
        "id", "broker_order_id", "broker_reference_id", "reference_id", "status",
        "trading_symbol", "exchange", "segment", "product", "transaction_type",
        "order_type", "quantity", "filled_quantity", "price", "average_fill_price",
    )
    return freeze_snapshot({
        field: getattr(order, field) for field in fields if hasattr(order, field)
    })


async def observe_order_failure(session, *, local, broker, reason, clock):
    return await observe(
        session,
        local={"mode": "PAPER", "orders": [order_snapshot(row) for row in local]},
        broker={"mode": "PAPER", "orders": [order_snapshot(row) for row in broker],
                "observation": reason},
        delta={"order_failure": reason, "automatic_resubmission": False},
        clock=clock,
    )


def snapshot(row):
    return freeze_snapshot({
        "id": row.id, "kind": row.kind, "local_state": row.local_state,
        "broker_state": row.broker_state, "delta": row.delta, "detail": row.detail,
        "resolved": row.resolved, "resolution": row.resolution,
        "resolved_by": row.resolved_by,
        "resolved_at": _utc(row.resolved_at) if row.resolved_at else None,
        "detected_at": _utc(row.detected_at),
    })


async def verify(session, row):
    records = list(await session.scalars(sa.select(AuditEvent)
        .where(AuditEvent.chain_id == row.id).order_by(AuditEvent.sequence)
        .limit(MAX_AUDIT_RECORDS + 1)))
    if len(records) > MAX_AUDIT_RECORDS:
        raise SafetyError("DISCREPANCY_EVIDENCE_CAPACITY_EXCEEDED")
    if (not records or not verify_records(records)
            or any(record.mode != TradingMode.PAPER for record in records)
            or records[-1].result.get("record") != snapshot(row)):
        raise SafetyError("DISCREPANCY_EVIDENCE_INVALID")
    return records


async def system_row(session):
    system = await session.get(SystemState, SINGLETON_ID, with_for_update=True)
    if system is None:
        system = SystemState(id=SINGLETON_ID, mode=TradingMode.PAPER, open_discrepancies=0)
        session.add(system)
        await session.flush()
    if system.mode != TradingMode.PAPER:
        raise SafetyError("PAPER_SYSTEM_REQUIRED")
    return system


async def append(session, row, clock, *, phase, actor, reason=None):
    record = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(chain_id=row.id, event_type=phase, actor=actor, mode=TradingMode.PAPER),
        {"result": {"record": snapshot(row), "reason": reason}},
    )
    session.add(RuntimeEventOutbox(audit_id=record.id))


async def observe(session, *, local, broker, delta, clock):
    system = await system_row(session)
    rows = list(await session.scalars(sa.select(Discrepancy).where(
        Discrepancy.kind == KIND, Discrepancy.resolved.is_(False)
    ).with_for_update()))
    if len(rows) > 1:
        raise SafetyError("AMBIGUOUS_DISCREPANCY_STATE")
    local, broker, delta = map(freeze_snapshot, (local, broker, delta))
    row = rows[0] if rows else None
    if row is not None:
        await verify(session, row)
        if (row.local_state, row.broker_state, row.delta) == (local, broker, delta):
            return row
    else:
        row = Discrepancy(
            kind=KIND, resolved=False, detected_at=clock.utcnow(),
            detail="PAPER evidence mismatch; explicit owner review required; no automatic repair",
        )
        session.add(row)
        system.open_discrepancies += 1
    row.local_state, row.broker_state, row.delta = local, broker, delta
    await session.flush()
    await append(
        session, row, clock, phase="PAPER_DISCREPANCY_OBSERVED", actor="paper_reconciliation"
    )
    return row


async def pending(session):
    rows = list(await session.scalars(sa.select(Discrepancy).where(
        Discrepancy.kind == KIND, Discrepancy.resolved.is_(False)
    )))
    for row in rows:
        await verify(session, row)
    return rows


async def resolve(worker, identifier, *, actor, reason, expected_head):
    if worker is None or not worker.running or not worker.executor.ready:
        raise SafetyError("RUNNING_RECOVERED_PAPER_WORKER_REQUIRED")
    executor = worker.executor
    if executor.settings.trading_mode != TradingMode.PAPER:
        raise SafetyError("PAPER_MODE_REQUIRED")
    async with worker.cycle_lock:
        await executor._reconcile(allow_review_pending=True)
        async with db_session.session_scope() as session:
            system = await system_row(session)
            row = await session.get(Discrepancy, identifier, with_for_update=True)
            if row is None or row.kind != KIND:
                raise SafetyError("PAPER_DISCREPANCY_REQUIRED")
            records = await verify(session, row)
            if not row.resolved:
                if records[-1].record_hash != expected_head:
                    raise SafetyError("DISCREPANCY_CHANGED_REVIEW_AGAIN")
                row.resolved = True
                row.resolution = reason
                row.resolved_by, row.resolved_at = actor, executor.clock.utcnow()
                system.open_discrepancies = max(0, system.open_discrepancies - 1)
                await append(session, row, executor.clock, phase="PAPER_DISCREPANCY_RESOLVED",
                             actor=actor, reason=reason)
                await session.flush()
            result = snapshot(row)
        await executor._reconcile(allow_review_pending=True)
        return result
