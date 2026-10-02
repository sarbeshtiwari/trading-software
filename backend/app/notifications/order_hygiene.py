"""Verified recorded order actions, not a claim of complete historical coverage."""

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.db.models.audit import AuditEvent
from app.modes import TradingMode


async def order_hygiene_snapshot(session, start, cutoff):
    rows = list(
        await session.scalars(
            sa.select(AuditEvent)
            .where(
                AuditEvent.mode == TradingMode.PAPER,
                AuditEvent.occurred_at >= start,
                AuditEvent.occurred_at <= cutoff,
                AuditEvent.event_type.in_(
                    ["ENTRY_CANCEL_INTENT", "ENTRY_CANCEL_RESULT", "PROTECTION_FAILURE"]
                ),
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.sequence)
            .limit(2001)
        )
    )
    if len(rows) > 2000:
        raise ValueError("order hygiene report exceeds supported scope")
    for identifier in {row.chain_id for row in rows}:
        records = list(
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == identifier,
                )
                .order_by(AuditEvent.sequence)
            )
        )
        if not verify_records(records):
            raise ValueError("order hygiene audit integrity failure")
    attempts = {}
    protection = []
    for row in rows:
        if row.event_type == "PROTECTION_FAILURE":
            protection.append(row.id)
        else:
            attempts[row.chain_id] = {
                "audit_id": row.id,
                "chain_id": row.chain_id,
                "order_id": row.order_id,
                "reason": row.result["reason"],
                "status": row.result.get("status", "RESULT_UNAVAILABLE"),
                "filled_quantity": row.result.get("filled_quantity"),
                "terminal": row.result.get("terminal"),
                "error": row.result.get("error"),
            }
    return {
        "cancellations": list(attempts.values()),
        "protection_failure_ids": protection,
        "scope": "Recorded session actions only; absence of failures is not proof of protection",
    }
