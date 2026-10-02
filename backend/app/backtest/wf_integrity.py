"""Internal report/audit binding, not external data or performance certification."""

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.backtest.catalog import catalog_digest, load_catalog
from app.db.models.audit import AuditEvent


async def report_integrity(session, row, *, catalog=None):
    if not row.simulated:
        return "INVALID"
    walkforward = row.kind == "WALKFORWARD"
    terminal = {"COMPLETED"} if walkforward else {"COMPLETED", "INCOMPLETE", "FAILED"}
    if row.status not in terminal:
        return "UNFINISHED"
    chain_id = f"wf:{row.id}" if walkforward else f"history:{row.id}"
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == chain_id)
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not events or not verify_records(events):
        return "INVALID"
    expected = "WALKFORWARD_COMPLETED" if walkforward else "HISTORICAL_RUN_FINISHED"
    if events[-1].event_type != expected:
        return "LEGACY_UNBOUND"
    if events[-1].result.get("catalog_sha256") != catalog_digest(
        catalog if catalog is not None else await load_catalog(session, row.id)
    ):
        return "INVALID"
    return "AUDIT_BOUND"
