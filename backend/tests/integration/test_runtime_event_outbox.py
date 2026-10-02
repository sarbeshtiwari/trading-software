"""Actual PAPER OMS commits reach Redis; ambiguous publication reuses event identity."""

import json

import pytest
import sqlalchemy as sa

from app.core.events import RedisStreamEventBus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.event_outbox import RuntimeEventOutbox
from app.execution import paper
from app.monitoring.runtime_events import RuntimeEventPublisher
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_redis_events import URL, redis_events

__all__ = ["credentials", "redis_events"]


@pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")
async def test_committed_oms_relay_and_ambiguous_publication(
    db_engine, credentials, fake_clock, redis_events, monkeypatch
):
    client, prefix = redis_events
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    bus = RedisStreamEventBus(client, stream_prefix=prefix)
    publisher = RuntimeEventPublisher(engine.settings, bus=bus, clock=fake_clock)
    try:
        order = await engine.submit(proposal)
        async with db_session.session_scope() as session:
            intents = list(await session.scalars(sa.select(RuntimeEventOutbox)))
        assert len(intents) >= 3
        assert all(row.published_at is None for row in intents)
        original = bus.publish

        async def lost_ack(event):
            await original(event)
            raise RuntimeError("fixture lost Redis reply")

        monkeypatch.setattr(bus, "publish", lost_ack)
        with pytest.raises(RuntimeError, match="lost Redis reply"):
            await publisher.publish_once()
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(RuntimeEventOutbox)
                    .where(RuntimeEventOutbox.published_at.is_not(None))
                )
                == 0
            )
        monkeypatch.setattr(bus, "publish", original)
        assert await publisher.publish_once() == len(intents)
        restarted = RuntimeEventPublisher(engine.settings, bus=bus, clock=fake_clock)
        assert await restarted.publish_once() == 0
        messages = []
        for suffix in ("execution.order_update", "execution.order_submitted"):
            messages.extend(
                json.loads(fields[b"data"])
                for _identifier, fields in await client.xrange(f"{prefix}:{suffix}")
            )
        assert len(messages) == len(intents) + 1
        assert len({message["id"] for message in messages}) == len(intents)
        assert all(message["payload"]["order_id"] == order for message in messages)
        assert all(message["payload"]["audit_chain_id"] == order for message in messages)
        assert all(message["payload"]["audit_sequence"] > 0 for message in messages)
        assert all(message["payload"]["execution_realism"] == "SIMULATED" for message in messages)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await api.aclose()


async def test_outbox_insert_failure_rolls_back_before_broker(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)

    def fail(**kwargs):
        raise RuntimeError("fixture outbox unavailable")

    monkeypatch.setattr(paper, "RuntimeEventOutbox", fail)
    try:
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "ORDER_CREATED")
                )
                == 0
            )
    finally:
        await api.aclose()
