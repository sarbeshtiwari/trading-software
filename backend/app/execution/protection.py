"""Read-only evidence of a software watchdog check, never a broker stop guarantee."""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.core.clock import UTC
from app.db.models.audit import AuditEvent


async def protection_status(session, position, now):
    rows = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == position.id)
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(rows):
        return "INTEGRITY_FAILURE"
    checks = [row for row in rows if row.event_type in {"PROTECTION_CHECKED", "PROTECTION_FAILURE"}]
    if not checks:
        return "UNAVAILABLE"
    latest = checks[-1]
    if latest.event_type == "PROTECTION_FAILURE":
        return "FAILED"
    try:
        result = latest.result
        if (
            result["quantity"] != position.net_quantity
            or Decimal(result["stop"]) != position.stop_loss_price
            or result["target"] != str(position.target_price)
        ):
            return "CHANGED_SINCE_CHECK"
        checked = latest.occurred_at
        checked = checked.replace(tzinfo=UTC) if checked.tzinfo is None else checked
        expires = datetime.fromisoformat(result["valid_until"])
        return "RECENT_SOFTWARE_CHECK" if checked <= now <= expires else "STALE"
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return "INVALID_EVIDENCE"
