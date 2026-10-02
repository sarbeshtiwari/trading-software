"""Immutable receipts for stored deterministic risk decisions, never authorization."""

import hashlib
from datetime import datetime

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical
from app.core.clock import get_clock
from app.db.models.audit import AuditEvent
from app.db.models.decision import RiskDecision


async def decision_digest(session, identifier):
    table = RiskDecision.__table__
    values = (
        (await session.execute(sa.select(table).where(table.c.id == identifier))).mappings().one()
    )
    snapshot = {
        key: _utc(value) if isinstance(value, datetime) else value
        for key, value in values.items()
        if key not in {"created_at", "updated_at"}
    }
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


async def bind_decision(session, row, *, clock=None, actor="risk_audit"):
    await session.flush()
    await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=row.id, event_type="RISK_DECISION_RECORDED", actor=actor, mode=row.mode
        ),
        {
            "risk_decision_id": row.id,
            "proposal_id": row.proposal_id,
            "result": {"risk_decision_sha256": await decision_digest(session, row.id)},
        },
        expected_count=0,
    )


async def decision_integrity(session, row):
    records = list(
        await session.scalars(
            sa.select(AuditEvent).where(AuditEvent.chain_id == row.id).order_by(AuditEvent.sequence)
        )
    )
    if not records:
        return "LEGACY_UNSEALED"
    if any(_utc(record.occurred_at) > get_clock().now() for record in records):
        return "FUTURE_EVIDENCE"
    if (
        len(records) != 1
        or not verify_records(records)
        or records[0].event_type != "RISK_DECISION_RECORDED"
        or records[0].risk_decision_id != row.id
        or records[0].proposal_id != row.proposal_id
        or records[0].mode != row.mode
        or (records[0].result or {}).get("risk_decision_sha256")
        != await decision_digest(session, row.id)
    ):
        return "CORRUPT"
    return "AUDIT_BOUND"
