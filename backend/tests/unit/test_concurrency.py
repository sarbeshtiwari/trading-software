"""Locks, event bus and shutdown.

Covers ARCH-011, ARCH-015, ARCH-016, ERR-006.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.core.clock import IST, FakeClock
from app.core.errors import ConfigurationError
from app.core.events import (
    Event,
    EventType,
    MemoryEventBus,
    create_event_bus,
)
from app.core.lifecycle import Lifecycle
from app.core.locks import (
    DistributedLock,
    LockAcquisitionError,
    MemoryLockStore,
    create_lock_store,
    trading_lock_key,
)

pytestmark = pytest.mark.unit


# --- ARCH-011 -------------------------------------------------------------


async def test_single_writer_lock() -> None:
    """Only one holder at a time: two trading loops on one account is a bug."""
    store = MemoryLockStore()
    key = trading_lock_key("primary")

    first = DistributedLock(store, key, ttl_seconds=60, owner="loop-a")
    await first.acquire()
    assert first.held

    second = DistributedLock(store, key, ttl_seconds=60, owner="loop-b")
    with pytest.raises(LockAcquisitionError) as excinfo:
        await second.acquire()
    assert key in str(excinfo.value)
    assert not second.held

    await first.release()
    assert not first.held

    # Once released, the second instance can take it.
    await second.acquire()
    assert second.held
    await second.release()
    await store.close()


async def test_lock_is_released_only_by_its_own_holder() -> None:
    store = MemoryLockStore()
    key = "ats:lock:test"

    assert await store.acquire(key, "token-a", 5000)
    # A different holder must not be able to release or extend it; without this
    # an expired lease could delete a lock a second process legitimately owns.
    assert not await store.release(key, "token-b")
    assert not await store.extend(key, "token-b", 5000)
    assert await store.owner(key) == "token-a"
    assert await store.release(key, "token-a")
    assert await store.owner(key) is None


async def test_lock_expires_after_its_ttl() -> None:
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 30, tzinfo=IST))
    store = MemoryLockStore(clock=clock)
    key = "ats:lock:test"

    assert await store.acquire(key, "token-a", 1000)
    assert not await store.acquire(key, "token-b", 1000)

    clock.advance_seconds(1.5)
    # The lease has expired, so another process may take over.
    assert await store.acquire(key, "token-b", 1000)


async def test_lock_context_manager_releases_on_error() -> None:
    store = MemoryLockStore()
    key = "ats:lock:test"

    with pytest.raises(RuntimeError):
        async with DistributedLock(store, key, ttl_seconds=5):
            raise RuntimeError("boom")

    assert await store.owner(key) is None


def test_memory_lock_store_refused_when_real_broker_reachable() -> None:
    # An in-process lock cannot stop a second *process* trading the account.
    with pytest.raises(ConfigurationError):
        create_lock_store("memory://", allow_memory=False)
    assert isinstance(create_lock_store("memory://"), MemoryLockStore)


# --- ARCH-016 -------------------------------------------------------------


async def test_event_bus_delivery_ack() -> None:
    bus = MemoryEventBus()
    await bus.start()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.SIGNAL, handler, name="collector")
    await bus.publish(Event(type=EventType.SIGNAL, payload={"symbol": "NIFTY"}))

    assert len(received) == 1
    assert received[0].payload["symbol"] == "NIFTY"
    assert not bus.dead_letters

    # A subscriber to a different type must not receive it.
    await bus.publish(Event(type=EventType.FILL, payload={}))
    assert len(received) == 1
    await bus.stop()


async def test_every_subscriber_receives_the_event() -> None:
    bus = MemoryEventBus()
    await bus.start()
    seen: list[str] = []

    async def first(event: Event) -> None:
        seen.append("first")

    async def second(event: Event) -> None:
        seen.append("second")

    bus.subscribe(EventType.ALERT, first, name="first")
    bus.subscribe(EventType.ALERT, second, name="second")
    await bus.publish(Event(type=EventType.ALERT))
    assert seen == ["first", "second"]


async def test_handler_failure_is_retried_then_dead_lettered() -> None:
    bus = MemoryEventBus(max_attempts=3)
    await bus.start()
    attempts: list[int] = []

    async def flaky(event: Event) -> None:
        attempts.append(1)
        raise ValueError("handler exploded")

    bus.subscribe(EventType.ORDER_UPDATE, flaky, name="flaky")
    await bus.publish(Event(type=EventType.ORDER_UPDATE, payload={"id": "ord_1"}))

    assert len(attempts) == 3
    assert len(bus.dead_letters) == 1
    event, handler_name, error = bus.dead_letters[0]
    assert event.payload["id"] == "ord_1"
    assert handler_name == "flaky"
    assert "exploded" in error


async def test_dead_letter_handling_does_not_block_other_handlers() -> None:
    bus = MemoryEventBus(max_attempts=1)
    await bus.start()
    delivered: list[str] = []

    async def broken(event: Event) -> None:
        raise RuntimeError("nope")

    async def working(event: Event) -> None:
        delivered.append(event.id)

    bus.subscribe(EventType.TICK, broken, name="broken")
    bus.subscribe(EventType.TICK, working, name="working")
    await bus.publish(Event(type=EventType.TICK))

    assert len(delivered) == 1
    assert len(bus.dead_letters) == 1


def test_event_roundtrips_through_serialisation() -> None:
    original = Event(type=EventType.RISK_REJECTED, payload={"rule": "daily_loss"})
    restored = Event.from_dict(original.to_dict())
    assert restored.type is EventType.RISK_REJECTED
    assert restored.payload == {"rule": "daily_loss"}
    assert restored.id == original.id


def test_create_event_bus_selects_memory_for_memory_url() -> None:
    assert isinstance(create_event_bus("memory://test"), MemoryEventBus)


# --- ARCH-015 -------------------------------------------------------------


async def test_graceful_shutdown() -> None:
    lifecycle = Lifecycle()
    order: list[str] = []

    async def close_db() -> None:
        order.append("db")

    async def release_lock() -> None:
        order.append("lock")

    def flush_audit() -> None:
        order.append("audit")

    lifecycle.on_shutdown(close_db, name="db")
    lifecycle.on_shutdown(flush_audit, name="audit")
    lifecycle.on_shutdown(release_lock, name="lock")

    assert not lifecycle.shutting_down
    await lifecycle.shutdown(reason="test")

    # Reverse registration order: the lock is released before the database closes.
    assert order == ["lock", "audit", "db"]
    assert lifecycle.shutting_down
    assert lifecycle.completed


async def test_shutdown_continues_after_a_failing_hook() -> None:
    lifecycle = Lifecycle()
    ran: list[str] = []

    async def explodes() -> None:
        raise RuntimeError("hook failed")

    async def must_still_run() -> None:
        ran.append("cleanup")

    lifecycle.on_shutdown(must_still_run, name="cleanup")
    lifecycle.on_shutdown(explodes, name="explodes")

    await lifecycle.shutdown()
    assert ran == ["cleanup"]
    assert lifecycle.completed


async def test_shutdown_hook_timeout_does_not_block_the_rest() -> None:
    lifecycle = Lifecycle()
    ran: list[str] = []

    async def hangs() -> None:
        await asyncio.sleep(10)

    async def quick() -> None:
        ran.append("quick")

    lifecycle.on_shutdown(quick, name="quick")
    lifecycle.on_shutdown(hangs, name="hangs", timeout_seconds=0.05)

    await lifecycle.shutdown()
    assert ran == ["quick"]


async def test_shutdown_is_idempotent() -> None:
    lifecycle = Lifecycle()
    calls: list[int] = []

    async def hook() -> None:
        calls.append(1)

    lifecycle.on_shutdown(hook, name="hook")
    await lifecycle.shutdown()
    await lifecycle.shutdown()
    assert len(calls) == 1
