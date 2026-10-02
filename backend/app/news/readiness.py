"""Shared point-in-time acquisition gate for every news evidence consumer."""

from datetime import timedelta

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.core.clock import UTC
from app.db.models.audit import AuditEvent
from app.news.polling import poll_chain


async def sources_available(session, sources, as_of, settings):
    if as_of.utcoffset() is None or not settings.news_enabled:
        return False
    cutoff = as_of.astimezone(UTC)
    for source in sources:
        records = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.chain_id == poll_chain(source["slug"]),
                        AuditEvent.occurred_at <= cutoff,
                    )
                    .order_by(AuditEvent.sequence)
                )
            ).all()
        )
        if not records:
            continue
        if not verify_records(records):
            return False
        latest = records[-1]
        from_time = latest.occurred_at
        from_time = from_time.replace(tzinfo=UTC) if from_time.tzinfo is None else from_time
        if (
            latest.event_type != "NEWS_POLL_FINISHED"
            or latest.result.get("status") != "ACQUIRED_UNVERIFIED"
            or cutoff - from_time > timedelta(seconds=settings.news_poll_interval_seconds)
        ):
            return False
    return True
