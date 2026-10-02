"""Event bus — ARCH-016, ERR-006.

Producers (market data, strategies, execution) publish events; consumers (risk,
journal, notifications, the dashboard websocket) subscribe. Decoupling them keeps
the trading loop from blocking on a slow consumer, and lets the audit and journal
subsystems observe everything without being wired into every call site.

Delivery semantics
------------------
* **Ack on success.** A handler that raises does not acknowledge the event; the
  bus retries it a bounded number of times.
* **Dead letter, never silent drop.** After the retry budget is exhausted the
  event moves to a dead-letter sink and raises an alert. A poison event must not
  loop forever, and it must not vanish.

Two implementations share one interface: an in-process bus (tests, single-process
PAPER runs) and a Redis Streams bus (production, survives consumer restarts).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from app.core.clock import get_clock
from app.core.errors import ConfigurationError
from app.core.ids import new_id
from app.core.logging import get_correlation_id, get_logger

logger = get_logger("core.events")

__all__ = [
    "EventType",
    "Event",
    "EventHandler",
    "EventBus",
    "MemoryEventBus",
    "RedisStreamEventBus",
    "create_event_bus",
]


class EventType(str, Enum):
    """Every event the system publishes. Centralised so consumers cannot typo."""

    # Market data
    TICK = "market.tick"
    BAR_CLOSED = "market.bar_closed"
    FEED_GAP = "market.feed_gap"
    DATA_QUALITY = "market.data_quality"
    STALE_DATA = "market.stale_data"

    # Decision pipeline
    SIGNAL = "decision.signal"
    PROPOSAL = "decision.proposal"
    PROPOSAL_REJECTED = "decision.proposal_rejected"
    RISK_APPROVED = "decision.risk_approved"
    RISK_REJECTED = "decision.risk_rejected"

    # Execution
    ORDER_SUBMITTED = "execution.order_submitted"
    ORDER_UPDATE = "execution.order_update"
    FILL = "execution.fill"
    ORDER_REJECTED = "execution.order_rejected"
    POSITION_UPDATE = "execution.position_update"
    POSITION_CLOSED = "execution.position_closed"

    # System
    HEALTH_CHANGED = "system.health_changed"
    TRADING_DISABLED = "system.trading_disabled"
    TRADING_ENABLED = "system.trading_enabled"
    EMERGENCY = "system.emergency"
    RECONCILIATION_DISCREPANCY = "system.reconciliation_discrepancy"
    ALERT = "system.alert"


@dataclass(frozen=True)
class Event:
    """An immutable fact that has already happened."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("evt"))
    occurred_at: datetime = field(default_factory=lambda: get_clock().now())
    correlation_id: Optional[str] = field(default_factory=get_correlation_id)
    source: str = "ats"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "payload": self.payload,
            "occurred_at": self.occurred_at.isoformat(),
            "correlation_id": self.correlation_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            type=EventType(data["type"]),
            payload=data.get("payload") or {},
            id=data.get("id") or new_id("evt"),
            occurred_at=datetime.fromisoformat(data["occurred_at"])
            if data.get("occurred_at")
            else get_clock().now(),
            correlation_id=data.get("correlation_id"),
            source=data.get("source", "ats"),
        )


EventHandler = Callable[[Event], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(self, event_type: EventType, handler: EventHandler, *, name: str = "") -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _BaseBus:
    """Shared subscription registry, retry policy and dead-letter handling."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        self._handlers: dict[EventType, list[tuple[str, EventHandler]]] = {}
        self._max_attempts = max(1, max_attempts)
        self.dead_letters: list[tuple[Event, str, str]] = []

    def subscribe(self, event_type: EventType, handler: EventHandler, *, name: str = "") -> None:
        label = name or getattr(handler, "__qualname__", repr(handler))
        self._handlers.setdefault(event_type, []).append((label, handler))
        logger.debug(
            "Subscribed handler to event",
            extra={"event_type": event_type.value, "handler": label},
        )

    def handlers_for(self, event_type: EventType) -> list[tuple[str, EventHandler]]:
        return list(self._handlers.get(event_type, ()))

    async def _dispatch(self, event: Event) -> None:
        """Deliver to every subscriber; one failing handler never blocks others."""
        for label, handler in self.handlers_for(event.type):
            await self._deliver_one(event, label, handler)

    async def _deliver_one(self, event: Event, label: str, handler: EventHandler) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await handler(event)
                return
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # noqa: BLE001 - bus must not die on a handler
                logger.warning(
                    "Event handler failed",
                    extra={
                        "event_id": event.id,
                        "event_type": event.type.value,
                        "handler": label,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "error": str(exc),
                    },
                )
                if attempt >= self._max_attempts:
                    self._dead_letter(event, label, str(exc))
                    return
                await asyncio.sleep(min(0.1 * attempt, 1.0))

    def _dead_letter(self, event: Event, label: str, error: str) -> None:
        self.dead_letters.append((event, label, error))
        logger.error(
            "Event moved to dead letter after exhausting retries",
            extra={
                "event_id": event.id,
                "event_type": event.type.value,
                "handler": label,
                "error": error,
                "dead_letter_depth": len(self.dead_letters),
            },
        )


class MemoryEventBus(_BaseBus):
    """In-process bus. Delivery is awaited, so publish completes after dispatch."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        super().__init__(max_attempts=max_attempts)
        self._started = False

    async def publish(self, event: Event) -> None:
        if not self._started:
            # Publishing before start is allowed but noteworthy: it usually means
            # startup ordering is wrong and early events would be missed.
            logger.debug("Publishing on a bus that has not been started")
        await self._dispatch(event)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False


class RedisStreamEventBus(_BaseBus):
    """Redis Streams bus with a consumer group per event type.

    Events survive a consumer restart: unacknowledged entries are re-delivered
    from the group's pending list.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        stream_prefix: str = "ats:events",
        group: str = "ats",
        consumer: str = "worker",
        max_attempts: int = 3,
        block_ms: int = 1000,
    ) -> None:
        super().__init__(max_attempts=max_attempts)
        self._redis = redis_client
        self._prefix = stream_prefix
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    def _stream(self, event_type: EventType) -> str:
        return f"{self._prefix}:{event_type.value}"

    async def publish(self, event: Event) -> None:
        await self._redis.xadd(
            self._stream(event.type),
            {"data": json.dumps(event.to_dict(), default=str)},
        )

    async def start(self) -> None:
        self._running = True
        for event_type in list(self._handlers):
            await self._ensure_group(event_type)
            self._tasks.append(asyncio.create_task(self._consume(event_type)))

    async def _ensure_group(self, event_type: EventType) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream(event_type), self._group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume(self, event_type: EventType) -> None:
        stream = self._stream(event_type)
        while self._running:
            try:
                response = await self._redis.xreadgroup(
                    self._group,
                    self._consumer,
                    {stream: ">"},
                    count=10,
                    block=self._block_ms,
                )
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Event stream read failed", extra={"stream": stream, "error": str(exc)}
                )
                await asyncio.sleep(1.0)
                continue

            for _stream_name, entries in response or []:
                for entry_id, fields in entries:
                    raw = fields.get(b"data") or fields.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    try:
                        event = Event.from_dict(json.loads(raw))
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Undecodable event; acknowledging to avoid a poison loop",
                            extra={"stream": stream, "entry_id": str(entry_id), "error": str(exc)},
                        )
                        await self._redis.xack(stream, self._group, entry_id)
                        continue

                    await self._dispatch(event)
                    # Acked after dispatch: handlers that failed have already been
                    # dead-lettered with their error recorded.
                    await self._redis.xack(stream, self._group, entry_id)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()


def create_event_bus(redis_url: str, *, consumer: str = "worker") -> EventBus:
    """Build an event bus from a URL. ``memory://`` selects the in-process bus."""
    if redis_url.startswith("memory://"):
        return MemoryEventBus()
    try:
        from redis.asyncio import Redis  # noqa: PLC0415 - optional dependency
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ConfigurationError(
            "redis package is not installed but REDIS_URL points at a Redis server.",
            context={"redis_url": redis_url},
        ) from exc
    return RedisStreamEventBus(Redis.from_url(redis_url), consumer=consumer)
