"""Groww websocket feed — GRW-021, GRW-022, GRW-023.

The documented feed exposes LTP, index values, market depth, equity and F&O order
updates, and F&O position updates, with a cap of **1000 concurrent
subscriptions**.

Three things this module is responsible for:

* **Budget.** Subscriptions are a finite resource. When the budget is full the
  lowest-priority subscription is evicted and the eviction is logged — rather
  than the newest instrument silently receiving no data.
* **Reconnection.** Disconnects happen. On reconnect the previous subscription
  set is restored, and the outage window is published as a ``FEED_GAP`` event so
  downstream components know their data has a hole in it (staleness alone would
  not tell them how long it lasted).
* **Isolation of the vendor SDK.** The transport is an interface; the SDK
  implementation is one of them. The SDK is synchronous and its consume loop
  blocks, so it runs in a worker thread and hands messages to asyncio.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from app.core.clock import Clock, get_clock
from app.core.errors import ConfigurationError
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.marketdata.models import InstrumentRef

logger = get_logger("brokers.groww.feed")

__all__ = [
    "FeedPriority",
    "Subscription",
    "SubscriptionBudget",
    "FeedTransport",
    "SdkFeedTransport",
    "GrowwFeedClient",
]

#: Documented cap.
MAX_SUBSCRIPTIONS = 1000


class FeedPriority(IntEnum):
    """Higher wins when the budget is full."""

    #: Instruments we hold a position in — losing their prices is unacceptable.
    POSITION = 40
    #: Instruments with a working order.
    ORDER = 30
    #: Instruments a strategy is actively evaluating.
    STRATEGY = 20
    #: Index levels and watchlist entries.
    WATCH = 10


@dataclass(frozen=True)
class Subscription:
    instrument: InstrumentRef
    priority: FeedPriority
    #: ``ltp``, ``depth`` or ``index``.
    kind: str = "ltp"

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.instrument.key}"


class SubscriptionBudget:
    """Keeps the subscribed set within the documented cap — GRW-022."""

    def __init__(self, limit: int = MAX_SUBSCRIPTIONS) -> None:
        self.limit = limit
        self._subscriptions: dict[str, Subscription] = {}
        self.evictions: list[Subscription] = []

    def __len__(self) -> int:
        return len(self._subscriptions)

    @property
    def available(self) -> int:
        return max(0, self.limit - len(self._subscriptions))

    def current(self) -> list[Subscription]:
        return list(self._subscriptions.values())

    def contains(self, subscription: Subscription) -> bool:
        return subscription.key in self._subscriptions

    def add(self, subscription: Subscription) -> tuple[bool, Optional[Subscription]]:
        """Add a subscription, evicting the lowest priority if necessary.

        Returns ``(added, evicted)``. A request that cannot outrank anything
        currently subscribed is refused rather than silently accepted.
        """
        existing = self._subscriptions.get(subscription.key)
        if existing is not None:
            if subscription.priority > existing.priority:
                self._subscriptions[subscription.key] = subscription
            return True, None

        if len(self._subscriptions) < self.limit:
            self._subscriptions[subscription.key] = subscription
            return True, None

        victim = min(self._subscriptions.values(), key=lambda item: item.priority)
        if victim.priority >= subscription.priority:
            logger.warning(
                "Feed subscription refused: budget full and nothing lower priority",
                extra={
                    "requested": subscription.instrument.key,
                    "priority": int(subscription.priority),
                    "limit": self.limit,
                },
            )
            return False, None

        del self._subscriptions[victim.key]
        self._subscriptions[subscription.key] = subscription
        self.evictions.append(victim)
        logger.warning(
            "Feed subscription evicted to make room",
            extra={
                "evicted": victim.instrument.key,
                "evicted_priority": int(victim.priority),
                "added": subscription.instrument.key,
                "added_priority": int(subscription.priority),
            },
        )
        return True, victim

    def remove(self, subscription: Subscription) -> bool:
        return self._subscriptions.pop(subscription.key, None) is not None


class FeedTransport(Protocol):
    """What the feed client needs from a websocket implementation."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def subscribe(self, subscriptions: Sequence[Subscription]) -> None: ...

    async def unsubscribe(self, subscriptions: Sequence[Subscription]) -> None: ...

    async def next_message(self) -> dict[str, Any]:
        """Await the next message. Raises on disconnect."""


class SdkFeedTransport:
    """Transport backed by ``growwapi.GrowwFeed``.

    .. warning::
       **Unverified against a live feed.** The SDK method names are documented
       (``subscribe_ltp``, ``subscribe_market_depth``, ``subscribe_*_order_updates``)
       but the message payload shapes are not published, so the normalisation
       below is a best reading. Listed in ``docs/LIMITATIONS.md``.
    """

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token
        self._feed: Any = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._consumer: Optional[asyncio.Task[None]] = None

    async def connect(self) -> None:
        try:
            from growwapi import GrowwAPI, GrowwFeed  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise ConfigurationError(
                "growwapi is not installed, so the websocket feed is unavailable. "
                "Install it (`pip install growwapi`) or run with REST polling only.",
            ) from exc

        api = await asyncio.to_thread(GrowwAPI, self._access_token)
        self._feed = await asyncio.to_thread(GrowwFeed, api)

    async def close(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        self._feed = None

    async def subscribe(self, subscriptions: Sequence[Subscription]) -> None:
        if self._feed is None:
            raise ConfigurationError("feed transport is not connected")
        for subscription in subscriptions:
            instrument = subscription.instrument
            if subscription.kind == "depth":
                method = self._feed.subscribe_market_depth
            elif subscription.kind == "index":
                method = self._feed.subscribe_index_value
            else:
                method = self._feed.subscribe_ltp
            await asyncio.to_thread(
                method,
                exchange=instrument.exchange.value,
                segment=instrument.segment.value,
                trading_symbol=instrument.trading_symbol,
                on_data_received=self._enqueue,
            )

    async def unsubscribe(self, subscriptions: Sequence[Subscription]) -> None:
        if self._feed is None:
            return
        for subscription in subscriptions:
            instrument = subscription.instrument
            method = {
                "depth": self._feed.unsubscribe_market_depth,
                "index": self._feed.unsubscribe_index_value,
            }.get(subscription.kind, self._feed.unsubscribe_ltp)
            await asyncio.to_thread(
                method,
                exchange=instrument.exchange.value,
                segment=instrument.segment.value,
                trading_symbol=instrument.trading_symbol,
            )

    def _enqueue(self, message: Any, metadata: Any = None) -> None:
        """Called from the SDK thread; hands the message to the event loop."""
        payload = {"message": message, "metadata": metadata}
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            logger.error("Feed queue full; dropping a message")

    async def next_message(self) -> dict[str, Any]:
        return await self._queue.get()


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class FeedStats:
    messages: int = 0
    reconnects: int = 0
    last_message_at: Optional[datetime] = None
    disconnected_at: Optional[datetime] = None
    total_gap_seconds: float = 0.0


class GrowwFeedClient:
    """Budgeted, self-healing wrapper around a feed transport."""

    def __init__(
        self,
        transport: FeedTransport,
        *,
        event_bus: Optional[EventBus] = None,
        budget: Optional[SubscriptionBudget] = None,
        clock: Optional[Clock] = None,
        max_backoff_seconds: float = 30.0,
        base_backoff_seconds: float = 0.5,
    ) -> None:
        self._transport = transport
        self._bus = event_bus
        self.budget = budget or SubscriptionBudget()
        self._clock = clock or get_clock()
        self._max_backoff = max_backoff_seconds
        self._base_backoff = base_backoff_seconds
        self._handlers: list[MessageHandler] = []
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self.stats = FeedStats()

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    # --- Subscriptions ----------------------------------------------------

    async def subscribe(self, subscriptions: Sequence[Subscription]) -> list[Subscription]:
        """Subscribe within budget. Returns what was actually subscribed."""
        accepted: list[Subscription] = []
        evicted: list[Subscription] = []

        for subscription in subscriptions:
            added, victim = self.budget.add(subscription)
            if added:
                accepted.append(subscription)
            if victim is not None:
                evicted.append(victim)

        if evicted:
            await self._transport.unsubscribe(evicted)
        if accepted:
            await self._transport.subscribe(accepted)
        return accepted

    async def unsubscribe(self, subscriptions: Sequence[Subscription]) -> None:
        removed = [s for s in subscriptions if self.budget.remove(s)]
        if removed:
            await self._transport.unsubscribe(removed)

    # --- Lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        await self._transport.connect()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._transport.close()

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                message = await self._transport.next_message()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure is a disconnect
                if not self._running:
                    return
                attempt += 1
                await self._handle_disconnect(exc, attempt)
                continue

            attempt = 0
            self.stats.messages += 1
            self.stats.last_message_at = self._clock.now()
            for handler in self._handlers:
                try:
                    await handler(message)
                except Exception as handler_error:  # noqa: BLE001
                    logger.error(
                        "Feed handler failed", extra={"error": str(handler_error)}
                    )

    async def _handle_disconnect(self, exc: BaseException, attempt: int) -> None:
        if self.stats.disconnected_at is None:
            self.stats.disconnected_at = self._clock.now()

        delay = min(self._base_backoff * (2 ** (attempt - 1)), self._max_backoff)
        logger.warning(
            "Feed disconnected; reconnecting",
            extra={"attempt": attempt, "delay_seconds": delay, "error": str(exc)},
        )
        await asyncio.sleep(delay)

        try:
            await self._transport.connect()
            # Restore exactly what was subscribed before the outage.
            current = self.budget.current()
            if current:
                await self._transport.subscribe(current)
        except Exception as reconnect_error:  # noqa: BLE001 - keep retrying
            logger.error(
                "Feed reconnect failed", extra={"error": str(reconnect_error)}
            )
            return

        await self._report_gap()

    async def _report_gap(self) -> None:
        """Publish the outage window so consumers know their data has a hole."""
        started = self.stats.disconnected_at
        self.stats.disconnected_at = None
        self.stats.reconnects += 1

        if started is None:
            return
        gap_seconds = (self._clock.now() - started).total_seconds()
        self.stats.total_gap_seconds += gap_seconds

        logger.warning(
            "Feed reconnected after a gap",
            extra={
                "gap_seconds": round(gap_seconds, 3),
                "subscriptions": len(self.budget),
                "reconnects": self.stats.reconnects,
            },
        )
        if self._bus is not None:
            await self._bus.publish(
                Event(
                    type=EventType.FEED_GAP,
                    payload={
                        "started_at": started.isoformat(),
                        "ended_at": self._clock.now().isoformat(),
                        "gap_seconds": gap_seconds,
                        "subscriptions_restored": len(self.budget),
                    },
                )
            )
