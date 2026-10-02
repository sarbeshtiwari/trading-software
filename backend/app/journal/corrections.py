"""Append contextual revisions without altering fills, economics or original seals."""

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.clock import get_clock
from app.core.ids import new_id
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry, JournalRevision
from app.journal.schemas import RevisionView
from app.portfolio.journal_integrity import bind_journal, journal_verified


def revision_snapshot(row):
    return RevisionView.model_validate(row).model_dump(mode="json")


async def version_history(session, row):
    own = await session.get(JournalRevision, row.id)
    if row.version != 1 and own is None:
        raise ValueError("journal revision linkage unavailable")
    root_id = own.root_id if own else row.id
    revisions = list(
        (
            await session.scalars(
                sa.select(JournalRevision)
                .where(JournalRevision.root_id == root_id)
                .order_by(JournalRevision.version)
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == root_id)
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(events):
        raise ValueError("journal revision audit integrity failure")
    recorded = [
        event.result["revision"] for event in events if event.event_type == "JOURNAL_CORRECTED"
    ]
    if recorded != [revision_snapshot(item) for item in revisions]:
        raise ValueError("journal revision evidence changed or missing")
    previous_id = root_id
    if revisions and not await journal_verified(session, root_id):
        raise ValueError("journal original seal unavailable")
    for version, revision in enumerate(revisions, start=2):
        entry = await session.get(JournalEntry, revision.entry_id)
        if (
            revision.version != version
            or revision.previous_id != previous_id
            or entry is None
            or entry.version != version
            or not await journal_verified(session, entry.id)
        ):
            raise ValueError("journal revision sequence or seal failure")
        previous_id = revision.entry_id
    return [RevisionView.model_validate(item) for item in revisions]


async def correct(session, row, body, actor):
    own = await session.get(JournalRevision, row.id)
    root_id = own.root_id if own else row.id
    root = await session.get(JournalEntry, root_id, with_for_update=True)
    if root is None or not await journal_verified(session, root_id):
        raise ValueError("corrections require an intact audited original")
    history = await version_history(session, row)
    latest_id = history[-1].entry_id if history else root_id
    if row.id != latest_id or row.version != body.expected_version:
        raise ValueError("journal revision conflict; reload the latest version")
    changes = body.model_dump(exclude={"expected_version", "reason"}, exclude_none=True)
    if ("plan_adherence" in changes and row.kind != "TRADE") or (
        "rejection_detail" in changes and row.kind != "REJECTION"
    ):
        raise ValueError("correction field does not apply to this journal kind")
    changes = freeze_snapshot(changes)
    if all(getattr(row, key) == value for key, value in changes.items()):
        raise ValueError("correction makes no change")
    entry = JournalEntry(
        **{
            column.name: getattr(row, column.name)
            for column in JournalEntry.__table__.columns
            if column.name not in {"id", "version", "created_at", "updated_at", "superseded_by"}
        }
    )
    entry.id, entry.version = new_id("jrn"), row.version + 1
    entry.created_at = get_clock().utcnow()
    for key, value in changes.items():
        setattr(entry, key, value)
    session.add(entry)
    await bind_journal(
        session,
        entry.id,
        get_clock(),
        mode=row.mode,
        event_type="JOURNAL_CORRECTION_RECORDED",
        actor=actor,
    )
    revision = JournalRevision(
        entry_id=entry.id,
        previous_id=row.id,
        root_id=root_id,
        version=entry.version,
        actor=actor,
        reason=freeze_snapshot(body.reason),
        changes=changes,
        occurred_at=get_clock().utcnow(),
    )
    session.add(revision)
    await session.flush()
    await AuditService().append_in_session(
        session,
        AuditIdentity(chain_id=root_id, event_type="JOURNAL_CORRECTED", actor=actor, mode=row.mode),
        {"result": {"revision": revision_snapshot(revision), "economic_evidence_changed": False}},
    )
    return entry
