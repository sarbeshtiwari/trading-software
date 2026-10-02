"""AUDIT_V1 binds every stored column except the digest itself."""

import hashlib
import hmac
from datetime import datetime

from app.audit.snapshots import canonical
from app.core.clock import UTC
from app.db.models.audit import AuditEvent


def record_payload(record: AuditEvent) -> dict:
    values = {}
    for column in AuditEvent.__table__.columns:
        if column.name == "record_hash":
            continue
        value = getattr(record, column.name)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        values[column.name] = value
    return values


def digest(record: AuditEvent) -> str:
    return hashlib.sha256(("AUDIT_V1\n" + canonical(record_payload(record))).encode()).hexdigest()


def verify_records(
    records, *, expected_count: int | None = None, expected_head: str | None = None
) -> bool:
    previous = None
    chain_id = None
    count = 0
    for count, record in enumerate(records, start=1):
        if chain_id is None:
            chain_id = record.chain_id
        if (
            record.chain_id != chain_id
            or record.sequence != count
            or record.previous_hash != previous
            or not hmac.compare_digest(digest(record), record.record_hash)
        ):
            return False
        previous = record.record_hash
    return (expected_count is None or expected_count == count) and (
        expected_head is None or expected_head == previous
    )
