"""Audit-backed notification requests and bounded durable delivery attempts."""

import asyncio
import hashlib
import logging
from datetime import timedelta

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode
from app.notifications.models import Notification
from app.notifications.routing import route_notification

logger = logging.getLogger(__name__)


async def enqueue(session, *, key, event_type, severity, message, clock, source_event):
    identifier = "ntf" + hashlib.sha256(key.encode()).hexdigest()[:37]
    existing = await session.scalar(
        sa.select(AuditEvent).where(
            AuditEvent.chain_id == identifier,
            AuditEvent.sequence == 1,
        )
    )
    if existing is not None:
        return identifier
    notification = Notification(
        event_id=identifier,
        event_type=event_type,
        severity=severity,
        message=message,
        occurred_at=clock.now(),
    )
    await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=identifier,
            event_type="NOTIFICATION_REQUESTED",
            actor="notification_outbox",
            mode=source_event.mode,
            severity=severity,
        ),
        {
            "order_id": source_event.order_id,
            "proposal_id": source_event.proposal_id,
            "position_id": source_event.position_id,
            "result": {
                "notification": notification.model_dump(mode="json"),
                "source_audit_id": source_event.id,
            },
        },
        expected_count=0,
    )
    return identifier


class NotificationOutbox:
    def __init__(self, service, *, clock=None):
        self.service = service
        self.clock = clock or get_clock()

    async def _requests(self):
        offset = 0
        batch = min(self.service.policy.queue_size, 100)
        while True:
            async with db_session.session_scope() as session:
                rows = list(
                    (
                        await session.scalars(
                            sa.select(AuditEvent)
                            .where(
                                AuditEvent.event_type == "NOTIFICATION_REQUESTED",
                                AuditEvent.mode == TradingMode.PAPER,
                                AuditEvent.occurred_at <= self.clock.utcnow(),
                            )
                            .order_by(AuditEvent.occurred_at, AuditEvent.id)
                            .offset(offset)
                            .limit(batch)
                        )
                    ).all()
                )
            for row in rows:
                yield row
            if len(rows) < batch:
                return
            offset += batch

    async def dispatch_once(self):
        processed = 0
        async for request in self._requests():
            if processed >= self.service.policy.queue_size:
                break
            notice = Notification.model_validate(request.result["notification"])
            routing = route_notification(notice.severity, self.clock.now(), self.service.routing)
            for channel in routing.channels:
                if processed >= self.service.policy.queue_size:
                    return processed
                if channel not in self.service.channels:
                    continue
                if await self._attempt(request, notice, channel):
                    processed += 1
        return processed

    async def _attempt(self, request, notice, channel):
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(
                            AuditEvent.chain_id == request.chain_id,
                        )
                        .order_by(AuditEvent.sequence)
                        .with_for_update()
                    )
                ).all()
            )
            if not verify_records(rows) or not rows or rows[0].id != request.id:
                raise ValueError("notification audit integrity failure")
            attempts = [
                row
                for row in rows
                if row.event_type == "NOTIFICATION_ATTEMPT" and row.result["channel"] == channel
            ]
            receipts = [
                row
                for row in rows
                if row.event_type == "NOTIFICATION_RECEIPT" and row.result["channel"] == channel
            ]
            if any(row.result["status"] == "ACKNOWLEDGED" for row in receipts):
                return False
            if len(attempts) >= self.service.policy.max_attempts:
                return False
            if attempts:
                last = attempts[-1]
                receipt = next(
                    (row for row in receipts if row.result["attempt_id"] == last.id), None
                )
                delay = (
                    self.service.policy.retry_delay_seconds
                    if receipt
                    else self.service.policy.timeout_seconds + 5
                )
                reference = (receipt or last).occurred_at
                reference = reference.replace(tzinfo=UTC) if reference.tzinfo is None else reference
                if self.clock.utcnow() < reference + timedelta(seconds=delay):
                    return False
            first_admission = not any(row.event_type == "NOTIFICATION_ATTEMPT" for row in rows)
            if (
                first_admission
                and self.service.limiter.check(notice, self.clock.monotonic()) != "ALLOW"
            ):
                return False
            attempt = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=request.chain_id,
                    event_type="NOTIFICATION_ATTEMPT",
                    actor="notification_outbox",
                    mode=request.mode,
                ),
                {"result": {"channel": channel, "number": len(attempts) + 1}},
                expected_count=len(rows),
            )
        if first_admission:
            self.service.limiter.admit(notice, self.clock.monotonic())
        try:
            await self.service.send_once(channel, notice)
            status = "ACKNOWLEDGED"
        except asyncio.CancelledError:
            raise
        except Exception:
            status = "FAILED"
            logger.warning(
                "Durable notification delivery failed",
                extra={
                    "channel": channel,
                    "event_id": notice.event_id,
                    "attempt": len(attempts) + 1,
                },
            )
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=request.chain_id,
                event_type="NOTIFICATION_RECEIPT",
                actor="notification_outbox",
                mode=request.mode,
            ),
            {"result": {"channel": channel, "attempt_id": attempt.id, "status": status}},
        )
        return True


async def recorded_states(session, now, *, limit=100):
    requests = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.event_type == "NOTIFICATION_REQUESTED",
                    AuditEvent.occurred_at <= now,
                    AuditEvent.mode == TradingMode.PAPER,
                )
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                .limit(limit)
            )
        ).all()
    )
    identifiers = [row.chain_id for row in requests]
    rows = (
        list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.chain_id.in_(identifiers),
                    )
                    .order_by(AuditEvent.chain_id, AuditEvent.sequence)
                )
            ).all()
        )
        if identifiers
        else []
    )
    result = []
    for request in requests:
        chain = [row for row in rows if row.chain_id == request.chain_id]
        attempts = [row for row in chain if row.event_type == "NOTIFICATION_ATTEMPT"]
        receipts = [row for row in chain if row.event_type == "NOTIFICATION_RECEIPT"]
        channels = {}
        for attempt in attempts:
            channel = attempt.result["channel"]
            if any(
                row.result["channel"] == channel and row.result["status"] == "ACKNOWLEDGED"
                for row in receipts
            ):
                channels[channel] = "ACKNOWLEDGED"
            else:
                receipt = next(
                    (row for row in receipts if row.result["attempt_id"] == attempt.id), None
                )
                channels[channel] = receipt.result["status"] if receipt else "IN_FLIGHT_OR_UNKNOWN"
        result.append(
            {
                "event_id": request.chain_id,
                "source_audit_id": request.result["source_audit_id"],
                "event_type": request.result["notification"]["event_type"],
                "requested_at": request.occurred_at,
                "order_id": request.order_id,
                "position_id": request.position_id,
                "status": ("RECORDED_CHANNEL_OUTCOMES" if attempts else "PENDING")
                if verify_records(chain)
                else "INTEGRITY_FAILURE",
                "channels": channels,
                "attempts": len(attempts),
                "external_delivery_verified": False,
            }
        )
    return result
