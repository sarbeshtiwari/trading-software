"""At-least-once relay of committed operational facts; never an execution command path."""

import asyncio
import logging

import sqlalchemy as sa
from redis.asyncio import Redis

from app.audit.integrity import verify_records
from app.core.clock import UTC, get_clock
from app.core.events import Event, RedisStreamEventBus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.event_outbox import RuntimeEventOutbox
from app.modes import TradingMode
from app.monitoring.event_types import RUNTIME_EVENT_TYPES, expected_actor

logger = logging.getLogger(__name__)


class RuntimeEventPublisher:
    def __init__(self, settings, *, bus=None, clock=None):
        self.settings = settings
        self.bus = bus
        self.client = None
        self.clock = clock or get_clock()
        self.task = None
        self.status = "NOT_RUNNING"

    async def publish_once(self):
        if self.settings.trading_mode != TradingMode.PAPER:
            return 0
        if self.bus is None:
            self.client = Redis.from_url(
                self.settings.redis_url, socket_connect_timeout=5, socket_timeout=2
            )
            self.bus = RedisStreamEventBus(self.client)
        published = 0
        for _attempt in range(50):
            async with db_session.session_scope() as session:
                pending = await session.scalar(
                    sa.select(RuntimeEventOutbox)
                    .where(RuntimeEventOutbox.published_at.is_(None))
                    .order_by(RuntimeEventOutbox.audit_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if pending is None:
                    break
                source = await session.get(AuditEvent, pending.audit_id)
                history = list(
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == source.chain_id)
                        .order_by(AuditEvent.sequence)
                    )
                )
                timestamp = source.occurred_at
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                if (
                    not verify_records(history)
                    or source.mode != TradingMode.PAPER
                    or source.actor != expected_actor(source.event_type, self.settings)
                    or timestamp > self.clock.utcnow()
                    or source.event_type not in RUNTIME_EVENT_TYPES
                ):
                    raise ValueError("invalid runtime event evidence")
                event = Event(
                    id=source.id,
                    occurred_at=timestamp,
                    source=source.actor,
                    type=RUNTIME_EVENT_TYPES[source.event_type],
                    payload={
                        "audit_id": source.id,
                        "audit_chain_id": source.chain_id,
                        "audit_sequence": source.sequence,
                        "audit_hash": source.record_hash,
                        "order_id": source.order_id,
                        "position_id": source.position_id,
                        "proposal_id": source.proposal_id,
                        "phase": source.event_type,
                        "fill_id": source.result.get("fill_id")
                        if source.event_type == "PAPER_FILL_RECORDED" else None,
                        "trading_mode": "PAPER",
                        "execution_realism": "SIMULATED"
                        if source.actor == "paper_execution" else None,
                    },
                )
                await asyncio.wait_for(self.bus.publish(event), timeout=6)
                pending.published_at = self.clock.utcnow()
                published += 1
        return published

    async def start(self):
        if self.settings.trading_mode != TradingMode.PAPER:
            self.status = "NOT_APPLICABLE"
        elif self.task is None:
            self.status = "STARTING"
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                await asyncio.wait_for(self.publish_once(), timeout=15)
                self.status = "PUBLISHING"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "RUNTIME_EVENT_PUBLICATION_UNAVAILABLE"
                logger.warning(
                    "Runtime event publication unavailable",
                    extra={"error_type": type(error).__name__},
                )
            await asyncio.sleep(1)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None
            self.bus = None
        self.status = "NOT_RUNNING"
