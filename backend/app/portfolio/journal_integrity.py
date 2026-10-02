"""Immutable economic journal evidence bound atomically to the fill transaction."""

import hashlib
from datetime import datetime

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.modes import TradingMode


async def journal_digest(session, identifier):
    table = JournalEntry.__table__
    values = (
        (await session.execute(sa.select(table).where(table.c.id == identifier))).mappings().one()
    )
    snapshot = {
        key: _utc(value) if isinstance(value, datetime) else value
        for key, value in values.items()
        if key not in {"created_at", "updated_at"}
    }
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


async def bind_journal(
    session,
    identifier,
    clock,
    *,
    mode=TradingMode.PAPER,
    event_type="PAPER_JOURNAL_RECORDED",
    actor="paper_execution",
):
    await session.flush()
    await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=identifier,
            event_type=event_type,
            actor=actor,
            mode=mode,
        ),
        {
            "result": {
                "journal_id": identifier,
                "journal_sha256": await journal_digest(session, identifier),
            }
        },
        expected_count=0,
    )


async def journal_verified(session, identifier):
    records = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == identifier)
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    return (
        bool(records)
        and verify_records(records)
        and (
            records[0].event_type
            in {
                "PAPER_JOURNAL_RECORDED",
                "REJECTION_JOURNAL_RECORDED",
                "ORDER_ACTION_JOURNAL_RECORDED",
                "JOURNAL_CORRECTION_RECORDED",
            }
            and records[0].result["journal_sha256"] == await journal_digest(session, identifier)
        )
    )
