"""Transactional chain writer; sequence collisions fail closed rather than overwrite."""

import sqlalchemy as sa
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.audit.integrity import digest, verify_records
from app.audit.snapshots import freeze_snapshot
from app.core.clock import Clock, get_clock
from app.core.enums import Severity
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode


class AuditIdentity(EvidenceModel):
    chain_id: str = Field(min_length=1, max_length=40)
    event_type: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=64)
    mode: TradingMode
    severity: Severity = Severity.INFO


class AuditService:
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or get_clock()

    async def append_in_session(
        self, session, identity: AuditIdentity, details: dict, *, expected_count: int | None = None
    ) -> AuditEvent:
        identity = AuditIdentity.model_validate(identity.model_dump())
        reserved = {
            "id",
            "sequence",
            "record_hash",
            "previous_hash",
            "occurred_at",
            *AuditIdentity.model_fields,
        }
        allowed = {column.name for column in AuditEvent.__table__.columns} - reserved
        if not set(details).issubset(allowed):
            raise ValueError("unknown or reserved audit fields")
        snapshot = freeze_snapshot(details)
        for column in AuditEvent.__table__.columns:
            value = snapshot.get(column.name)
            length = getattr(column.type, "length", None)
            if length and value is not None and (not isinstance(value, str) or len(value) > length):
                raise ValueError("invalid audit text field")
        records = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == identity.chain_id)
                    .order_by(AuditEvent.sequence)
                    .with_for_update()
                )
            ).all()
        )
        if not verify_records(records):
            raise ValueError("audit chain integrity failure")
        if expected_count is not None and len(records) != expected_count:
            raise ValueError("audit chain changed concurrently")
        head = records[-1] if records else None
        row = AuditEvent(
            id=new_id("aud"),
            **identity.model_dump(),
            occurred_at=self.clock.utcnow(),
            sequence=head.sequence + 1 if head else 1,
            previous_hash=head.record_hash if head else None,
            **{key: snapshot.get(key) for key in allowed},
        )
        row.record_hash = digest(row)
        session.add(row)
        await session.flush()
        return row

    async def append(self, identity: AuditIdentity, details: dict) -> str:
        async with db_session.session_scope() as session:
            row = await self.append_in_session(session, identity, details)
            return row.id

    async def chain(self, chain_id: str) -> list[AuditEvent]:
        async with db_session.session_scope() as session:
            return list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == chain_id)
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )

    async def verify(self, chain_id: str, *, expected_count=None, expected_head=None) -> bool:
        records = await self.chain(chain_id)
        return bool(records) and verify_records(
            records, expected_count=expected_count, expected_head=expected_head
        )
