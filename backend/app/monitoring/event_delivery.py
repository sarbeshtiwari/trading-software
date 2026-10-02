"""Restart-safe publication of retained failures into existing audit and notification services."""

import asyncio
import hashlib
import logging
import re

from redis.asyncio import Redis

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.core.enums import Severity
from app.db import session as db_session
from app.monitoring.event_evidence import summarize_failure
from app.notifications.outbox import enqueue

logger = logging.getLogger(__name__)


class EventFailureMonitor:
    def __init__(self, settings, *, client=None, stream="ats:events:dead-letter", clock=None):
        self.settings = settings
        self.client = client
        self.owns_client = client is None
        self.stream = stream
        self.clock = clock or get_clock()
        identity = f"{settings.redis_url}|{stream}|{settings.trading_mode.value}"
        self.chain_id = "efm" + hashlib.sha256(identity.encode()).hexdigest()[:37]
        self.task = None
        self.status = "NOT_RUNNING"

    async def publish_once(self):
        audit = AuditService(self.clock)
        history = await audit.chain(self.chain_id)
        if not verify_records(history):
            raise ValueError("event cursor integrity failure")
        if any(
            row.mode != self.settings.trading_mode
            or row.actor != "event_failure_monitor"
            or row.event_type != "EVENT_FAILURE_RECORDED"
            or (
                row.occurred_at.replace(tzinfo=UTC)
                if row.occurred_at.tzinfo is None
                else row.occurred_at
            )
            > self.clock.utcnow()
            for row in history
        ):
            raise ValueError("invalid event cursor provenance")
        cursor = history[-1].result["receipt"]["receipt_id"] if history else "0-0"
        if not re.fullmatch(r"[0-9]+-[0-9]+", cursor):
            raise ValueError("invalid event cursor")
        if self.client is None:
            self.client = Redis.from_url(
                self.settings.redis_url, socket_connect_timeout=5, socket_timeout=2
            )
        records = await self.client.xrange(self.stream, min=f"({cursor}", max="+", count=50)
        async with db_session.session_scope() as session:
            for offset, (identifier, fields) in enumerate(records):
                receipt = summarize_failure(identifier, fields)
                source = await audit.append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=self.chain_id,
                        event_type="EVENT_FAILURE_RECORDED",
                        actor="event_failure_monitor",
                        mode=self.settings.trading_mode,
                        severity=Severity.CRITICAL,
                    ),
                    {"result": {"receipt": receipt.model_dump(mode="json")}},
                    expected_count=len(history) + offset,
                )
                await enqueue(
                    session,
                    key=f"{self.chain_id}:{receipt.receipt_id}",
                    event_type="EVENT_DELIVERY_FAILURE",
                    severity=Severity.CRITICAL,
                    message=(
                        f"Retained event delivery failure {receipt.receipt_id}; inspect Monitoring."
                    ),
                    clock=self.clock,
                    source_event=source,
                )
        return len(records)

    async def start(self):
        if self.task is None:
            self.status = "STARTING"
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                await asyncio.wait_for(self.publish_once(), timeout=10)
                self.status = "PUBLISHING"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "EVENT_PUBLICATION_UNAVAILABLE"
                logger.warning(
                    "Event failure publication unavailable",
                    extra={
                        "error_type": type(error).__name__,
                    },
                )
            await asyncio.sleep(5)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
        self.status = "NOT_RUNNING"
