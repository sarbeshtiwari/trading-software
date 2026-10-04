"""Optional configured lifecycle and nonblocking critical-event submission."""

import asyncio
import logging
from dataclasses import dataclass

from app.notifications.channels import configured_channels
from app.notifications.dedupe import SuppressionPolicy
from app.notifications.models import DeliveryPolicy, Notification
from app.notifications.outbox import NotificationOutbox
from app.notifications.routing import ChannelRoute, RoutingPolicy
from app.notifications.service import NotificationService

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    service: NotificationService | None = None
    outbox_task: asyncio.Task | None = None
    outbox_stop: asyncio.Event | None = None
    outbox_observed: bool = False
    outbox_failed: bool = False


_runtime = Runtime()


def current_service():
    return _runtime.service


def status():
    service = _runtime.service
    if service is None:
        return {"status": "DISABLED", "detail": "Notification runtime unavailable or disabled"}
    if not service.running or _runtime.outbox_task is None or _runtime.outbox_task.done():
        return {"status": "DEGRADED", "detail": "Notification delivery task not running"}
    if not service.channels:
        return {"status": "DEGRADED", "detail": "No notification channel configured"}
    if _runtime.outbox_failed:
        return {
            "status": "DEGRADED",
            "detail": "Notification outbox unavailable; no success assumed",
        }
    if not _runtime.outbox_observed:
        return {"status": "STARTING", "detail": "Notification outbox not yet observed"}
    return {"status": "RUNNING", "detail": "Provider delivery is not externally verified"}


async def stop_notifications():
    task, _runtime.outbox_task = _runtime.outbox_task, None
    stopping, _runtime.outbox_stop = _runtime.outbox_stop, None
    if stopping is not None:
        stopping.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Notification shutdown timed out; unfinished attempts remain auditable")
        except asyncio.CancelledError:
            pass
    service, _runtime.service = _runtime.service, None
    if service is not None:
        await service.stop()
    _runtime.outbox_observed = False
    _runtime.outbox_failed = False


async def start_notifications(settings):
    await stop_notifications()
    if not settings.notification_enabled:
        logger.warning("Remote notifications disabled by configuration")
        return None
    try:
        routing = RoutingPolicy(
            routes=tuple(
                ChannelRoute(channel=name, severities=severities)
                for name, severities in settings.notification_routes.items()
            ),
            quiet_start=settings.notification_quiet_start or None,
            quiet_end=settings.notification_quiet_end or None,
        )
        delivery = DeliveryPolicy(
            queue_size=settings.notification_queue_size,
            max_attempts=settings.notification_max_attempts,
            timeout_seconds=settings.notification_timeout_seconds,
            retry_delay_seconds=settings.notification_retry_delay_seconds,
        )
        suppression = SuppressionPolicy(
            window_seconds=settings.notification_window_seconds,
            max_events_per_severity=settings.notification_max_events_per_severity,
            max_conditions=settings.notification_max_conditions,
        )
        service = NotificationService(
            configured_channels(settings, timeout=delivery.timeout_seconds),
            routing,
            delivery,
            suppression=suppression,
        )
        await service.start()
        _runtime.service = service
        _runtime.outbox_stop = asyncio.Event()
        _runtime.outbox_task = asyncio.create_task(_deliver_outbox(service, _runtime.outbox_stop))
        return service
    except Exception:
        logger.error("Remote notifications disabled: invalid configuration or startup failure")
        return None


async def _deliver_outbox(service, stopping):
    outbox = NotificationOutbox(service)
    while not stopping.is_set():
        try:
            await outbox.dispatch_once()
            _runtime.outbox_observed = True
            _runtime.outbox_failed = False
        except asyncio.CancelledError:
            raise
        except Exception:
            _runtime.outbox_observed = True
            _runtime.outbox_failed = True
            logger.error("Notification outbox unavailable; pending notices remain unacknowledged")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def emit_critical(*, event_id, event_type, message, condition_key, occurred_at):
    try:
        if _runtime.service is None:
            return "DISABLED"
        return _runtime.service.submit(
            Notification(
                event_id=event_id,
                event_type=event_type,
                severity="CRITICAL",
                message=message,
                condition_key=condition_key,
                occurred_at=occurred_at,
            )
        )
    except Exception:
        logger.error("Critical notification submission failed; local safety remains authoritative")
        return "FAILED"
