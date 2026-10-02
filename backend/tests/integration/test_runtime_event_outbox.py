"""Actual PAPER OMS commits reach Redis; ambiguous publication reuses event identity."""

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason
from app.core.events import RedisStreamEventBus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.trading import Order, Position, Trade
from app.execution import paper
from app.execution.event_types import PAPER_EVENT_TYPES
from app.monitoring.runtime_events import RuntimeEventPublisher
from app.portfolio import fill_evidence
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
        for suffix in dict.fromkeys(kind.value for kind in PAPER_EVENT_TYPES.values()):
            messages.extend(
                json.loads(fields[b"data"])
                for _identifier, fields in await client.xrange(f"{prefix}:{suffix}")
            )
        assert len(messages) == len(intents) + 1
        assert len({message["id"] for message in messages}) == len(intents)
        assert all(message["payload"]["order_id"] == order for message in messages)
        assert all(message["payload"]["audit_chain_id"] == order for message in messages
                   if message["type"] != "execution.fill")
        assert any(message["type"] == "execution.fill" and message["payload"]["fill_id"]
                   for message in messages)
        assert any(message["type"] == "execution.position_update" for message in messages)
        assert all(message["payload"]["audit_sequence"] > 0 for message in messages)
        assert all(message["payload"]["execution_realism"] == "SIMULATED" for message in messages)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await api.aclose()


@pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")
async def test_partial_fill_restart_and_exit_publish_once(
    db_engine, credentials, fake_clock, redis_events
):
    client, prefix = redis_events
    engine, proposal, market, api, _context = await setup_execution(credentials, fake_clock)
    bus = RedisStreamEventBus(client, stream_prefix=prefix)
    try:
        market["quantities"] = [10000, 10000, 100]
        identifier = await engine.submit(proposal)
        restarted = paper.PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restarted.recover()
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 100
        await restarted.sync(identifier)
        market.update(quantity=10000, bid=Decimal("98"), ask=Decimal("98.05"))
        exit_id = await restarted.exit(position.id, ExitReason.EMERGENCY)
        await restarted.sync(exit_id)
        publisher = RuntimeEventPublisher(engine.settings, bus=bus, clock=fake_clock)
        assert await publisher.publish_once() > 0
        assert await RuntimeEventPublisher(engine.settings, bus=bus, clock=fake_clock).publish_once() == 0
        fills = [json.loads(fields[b"data"]) for _identifier, fields in await client.xrange(f"{prefix}:execution.fill")]
        updates = await client.xrange(f"{prefix}:execution.position_update")
        closes = await client.xrange(f"{prefix}:execution.position_closed")
        assert len(fills) == 2
        assert len({event["payload"]["fill_id"] for event in fills}) == 2
        assert len(updates) == len(closes) == 1
        assert all(event["payload"]["position_id"] == position.id for event in fills)
        async with db_session.session_scope() as session:
            closed = await session.get(Position, position.id)
            assert closed.net_quantity == 0 and closed.realised_pnl == Decimal("-200")
        assert len(await restarted.broker.list_orders()) == 2
    finally:
        await api.aclose()


async def test_fill_publication_intent_failure_rolls_back_accounting_and_recovers(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    original = fill_evidence.RuntimeEventOutbox

    def fail(**kwargs):
        raise RuntimeError("fixture fill intent failure")

    monkeypatch.setattr(fill_evidence, "RuntimeEventOutbox", fail)
    try:
        with pytest.raises(RuntimeError, match="fill intent failure"):
            await engine.submit(proposal)
        assert len(await engine.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
            identifier = await session.scalar(sa.select(Order.id))
        monkeypatch.setattr(fill_evidence, "RuntimeEventOutbox", original)
        restarted = paper.PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restarted.recover()
        await restarted.sync(identifier)
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 1
            fill_intents = await session.scalar(sa.select(sa.func.count()).select_from(RuntimeEventOutbox)
                .join(AuditEvent, AuditEvent.id == RuntimeEventOutbox.audit_id)
                .where(AuditEvent.event_type == "PAPER_FILL_RECORDED"))
            assert fill_intents == 1
        assert len(await restarted.broker.list_orders()) == 1
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
