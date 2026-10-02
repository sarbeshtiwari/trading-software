"""Real Redis retention reaches atomic audit/outbox cursor and survives restart/failure."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.monitoring import event_delivery
from app.monitoring.event_delivery import EventFailureMonitor
from tests.integration.test_auth import credentials
from tests.integration.test_redis_events import URL, redis_events

__all__ = ["credentials", "redis_events"]
pytestmark = pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")


async def test_atomic_cursor_notifications_restart_and_tamper(
    db_engine, credentials, redis_events, fake_clock, monkeypatch
):
    client, prefix = redis_events
    stream = f"{prefix}:dead-letter"
    monitor = EventFailureMonitor(credentials[0], client=client, stream=stream, clock=fake_clock)
    fields = {
        "stream": f"{prefix}:execution.fill",
        "entry_id": "1-0",
        "handler": "test",
        "reason": "ValueError",
        "data": "private test payload",
    }
    await client.xadd(stream, fields)
    assert await monitor.publish_once() == 1
    restarted = EventFailureMonitor(credentials[0], client=client, stream=stream, clock=fake_clock)
    assert await restarted.publish_once() == 0
    assert await AuditService(fake_clock).verify(monitor.chain_id)
    await client.xadd(stream, fields)
    original = event_delivery.enqueue
    monkeypatch.setattr(
        event_delivery, "enqueue", AsyncMock(side_effect=RuntimeError("fixture DB failure"))
    )
    with pytest.raises(RuntimeError, match="fixture DB failure"):
        await restarted.publish_once()
    assert len(await AuditService(fake_clock).chain(monitor.chain_id)) == 1
    monkeypatch.setattr(event_delivery, "enqueue", original)
    assert await restarted.publish_once() == 1
    async with db_session.session_scope() as session:
        notices = list(
            await session.scalars(
                sa.select(AuditEvent).where(AuditEvent.event_type == "NOTIFICATION_REQUESTED")
            )
        )
        assert len(notices) == 2
        assert all("private test payload" not in str(row.result) for row in notices)
        cursor = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.chain_id == monitor.chain_id, AuditEvent.sequence == 2
            )
        )
        cursor.result = {"receipt": {"receipt_id": "9999999999999-0"}}
    with pytest.raises(ValueError, match="integrity"):
        await restarted.publish_once()


async def test_background_publisher_lifecycle(db_engine, credentials, redis_events, fake_clock):
    client, prefix = redis_events
    stream = f"{prefix}:dead-letter"
    await client.xadd(
        stream,
        {
            "stream": f"{prefix}:market.tick",
            "entry_id": "1-0",
            "handler": "decoder",
            "reason": "INVALID_ENVELOPE",
            "data": "malformed test",
        },
    )
    monitor = EventFailureMonitor(credentials[0], client=client, stream=stream, clock=fake_clock)
    await monitor.start()
    task = monitor.task
    await monitor.start()
    assert monitor.task is task
    try:

        async def published():
            while monitor.status != "PUBLISHING":
                await asyncio.sleep(0.02)

        await asyncio.wait_for(published(), 5)
        assert len(await AuditService(fake_clock).chain(monitor.chain_id)) == 1
    finally:
        await monitor.stop()
    assert monitor.status == "NOT_RUNNING"
    assert monitor.task is None
