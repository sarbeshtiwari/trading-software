"""Bounded, nonblocking enqueue; delivery failures never reach the trading caller."""

import asyncio
import logging
from collections import deque

from app.audit.snapshots import freeze_snapshot
from app.core.clock import get_clock
from app.notifications.dedupe import NotificationLimiter, SuppressionPolicy
from app.notifications.models import DeliveryPolicy, Notification
from app.notifications.routing import RoutingPolicy, route_notification

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(
        self,
        channels,
        routing: RoutingPolicy,
        policy: DeliveryPolicy,
        *,
        clock=None,
        suppression: SuppressionPolicy | None = None,
    ):
        self.channels = dict(channels)
        self.routing = RoutingPolicy.model_validate(routing.model_dump())
        self.policy = DeliveryPolicy.model_validate(policy.model_dump())
        self.clock = clock or get_clock()
        self._queue = asyncio.Queue(maxsize=policy.queue_size)
        self._worker = None
        self.outcomes = deque(maxlen=1000)
        self.limiter = NotificationLimiter(suppression or SuppressionPolicy())

    async def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def submit(self, notification):
        try:
            notification = Notification.model_validate(freeze_snapshot(notification.model_dump()))
            if notification.occurred_at > self.clock.now():
                raise ValueError("notification is future dated")
            if self._worker is None or self._worker.done():
                return "NOT_RUNNING"
            now = self.clock.monotonic()
            admission = self.limiter.check(notification, now)
            if admission != "ALLOW":
                return admission
            self._queue.put_nowait(notification)
            self.limiter.admit(notification, now)
            return "QUEUED"
        except asyncio.QueueFull:
            logger.error("Notification queue full; event not queued")
            return "QUEUE_FULL"
        except Exception:
            logger.error("Invalid notification rejected")
            return "INVALID"

    async def drain(self):
        await self._queue.join()

    async def stop(self):
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        while not self._queue.empty():
            notification = self._queue.get_nowait()
            self.outcomes.append((notification.event_id, None, "CANCELLED", 0))
            self._queue.task_done()

    async def _run(self):
        while True:
            notification = await self._queue.get()
            try:
                routing = route_notification(notification.severity, self.clock.now(), self.routing)
                if not routing.channels:
                    self.outcomes.append((notification.event_id, None, "NOT_ROUTED", 0))
                await asyncio.gather(
                    *(self._deliver(name, notification) for name in routing.channels)
                )
            except asyncio.CancelledError:
                self.outcomes.append((notification.event_id, None, "CANCELLED", 0))
                raise
            except Exception:
                logger.error("Notification worker failed to process event")
                self.outcomes.append((notification.event_id, None, "FAILED", 0))
            finally:
                self._queue.task_done()

    async def _deliver(self, name, notification):
        channel = self.channels.get(name)
        if channel is None:
            self.outcomes.append((notification.event_id, name, "DISABLED", 0))
            return
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                await self.send_once(name, notification)
                self.outcomes.append((notification.event_id, name, "ACKNOWLEDGED", attempt))
                return
            except Exception:
                logger.warning(
                    "Notification delivery failed", extra={"channel": name, "attempt": attempt}
                )
                if attempt < self.policy.max_attempts:
                    await asyncio.sleep(self.policy.retry_delay_seconds)
        self.outcomes.append((notification.event_id, name, "FAILED", self.policy.max_attempts))

    async def send_once(self, name, notification):
        notification = Notification.model_validate(freeze_snapshot(notification.model_dump()))
        if notification.occurred_at > self.clock.now():
            raise ValueError("notification is future dated")
        await asyncio.wait_for(self.channels[name].send(notification), self.policy.timeout_seconds)
