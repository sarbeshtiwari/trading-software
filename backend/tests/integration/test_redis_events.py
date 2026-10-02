"""Real Redis pending/retry/poison recovery in isolated, uniquely prefixed streams."""

import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.events import Event, EventType, RedisStreamEventBus

URL = os.environ.get("ATS_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")


@pytest.fixture
async def redis_events():
    client = Redis.from_url(URL)
    prefix = f"ats:test:events:{uuid4().hex}"
    await client.ping()
    try:
        yield client, prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def bus(client, prefix, consumer):
    return RedisStreamEventBus(
        client,
        stream_prefix=prefix,
        consumer=consumer,
        claim_idle_ms=100,
        block_ms=20,
        handler_timeout_seconds=1,
    )


async def drained(client, stream):
    async def wait():
        while (await client.xpending(stream, "ats"))["pending"]:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), 5)


async def test_new_consumer_recovers_interrupted_delivery(redis_events):
    client, prefix = redis_events
    original = bus(client, prefix, "before-restart")
    entered = asyncio.Event()

    async def interrupted(event):
        entered.set()
        await asyncio.Event().wait()

    original.subscribe(EventType.SIGNAL, interrupted, name="strategy")
    await original.start()
    event = Event(type=EventType.SIGNAL, payload={"fixture": True})
    try:
        await original.publish(event)
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        await original.stop()
    stream = f"{prefix}:decision.signal"
    assert (await client.xpending(stream, "ats"))["pending"] == 1
    recovered = bus(client, prefix, "after-restart")
    received = []

    async def handler(event):
        received.append(event.id)

    recovered.subscribe(EventType.SIGNAL, handler, name="strategy")
    await recovered.start()
    try:
        await drained(client, stream)
        assert received == [event.id]
    finally:
        await recovered.stop()


async def test_failed_handler_retained_and_other_handler_delivered(redis_events):
    client, prefix = redis_events
    worker = bus(client, prefix, "worker")
    calls, received = [], []

    async def broken(event):
        calls.append(event.id)
        raise ValueError("fixture failure")

    async def healthy(event):
        received.append(event.id)

    worker.subscribe(EventType.FILL, broken, name="broken")
    worker.subscribe(EventType.FILL, healthy, name="healthy")
    await worker.start()
    try:
        event = Event(type=EventType.FILL, payload={"fixture": True})
        await worker.publish(event)

        async def retained():
            while not await client.xlen(f"{prefix}:dead-letter"):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(retained(), 5)
        await drained(client, f"{prefix}:execution.fill")
        records = await client.xrange(f"{prefix}:dead-letter")
        assert len(records) == 1
        assert records[0][1][b"handler"] == b"broken"
        assert calls == [event.id] * 3
        assert received == [event.id]
    finally:
        await worker.stop()


async def test_poison_retention_failure_keeps_pending_for_restart(redis_events):
    client, prefix = redis_events
    worker = bus(client, prefix, "first")
    received = []

    async def handler(event):
        received.append(event.id)

    worker.subscribe(EventType.TICK, handler, name="collector")
    await client.set(f"{prefix}:dead-letter", "fixture wrong type")
    await worker.start()
    stream = f"{prefix}:market.tick"
    try:
        await client.xadd(stream, {"data": b"\xffinvalid-json"})

        async def pending():
            while not (await client.xpending(stream, "ats"))["pending"]:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(pending(), 5)
        await asyncio.sleep(0.1)
        assert (await client.xpending(stream, "ats"))["pending"] == 1
    finally:
        await worker.stop()
    await client.delete(f"{prefix}:dead-letter")
    restarted = bus(client, prefix, "second")
    restarted.subscribe(EventType.TICK, handler, name="collector")
    await restarted.start()
    try:
        await drained(client, stream)
        records = await client.xrange(f"{prefix}:dead-letter")
        assert records[0][1][b"data"] == b"\xffinvalid-json"
        assert records[0][1][b"reason"] == b"INVALID_ENVELOPE"
        assert not received
    finally:
        await restarted.stop()


async def test_retry_budget_survives_failed_retention_and_restart(redis_events):
    client, prefix = redis_events
    calls = []

    async def broken(event):
        calls.append(event.id)
        raise ValueError("fixture failure")

    await client.set(f"{prefix}:dead-letter", "fixture wrong type")
    first = bus(client, prefix, "first")
    first.subscribe(EventType.SIGNAL, broken, name="consumer")
    await first.start()
    try:
        await first.publish(Event(type=EventType.SIGNAL))

        async def exhausted():
            while len(calls) < 3:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(exhausted(), 5)
        await asyncio.sleep(0.1)
        assert (await client.xpending(f"{prefix}:decision.signal", "ats"))["pending"] == 1
    finally:
        await first.stop()
    await client.delete(f"{prefix}:dead-letter")
    second = bus(client, prefix, "second")
    second.subscribe(EventType.SIGNAL, broken, name="consumer")
    await second.start()
    try:
        await drained(client, f"{prefix}:decision.signal")
        assert len(calls) == 3
        assert await client.xlen(f"{prefix}:dead-letter") == 1
    finally:
        await second.stop()
