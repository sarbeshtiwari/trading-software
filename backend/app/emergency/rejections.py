"""Distinct observed PAPER broker rejections trip the existing durable entry control."""

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST
from app.db.models.audit import AuditEvent
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode
from app.notifications.outbox import enqueue


async def observe_rejection(session, order, source, clock, limit):
    previous = await session.scalar(
        sa.select(AuditEvent)
        .where(
            AuditEvent.event_type == "BROKER_REJECTION_OBSERVED",
            AuditEvent.order_id == order.id,
            AuditEvent.mode == TradingMode.PAPER,
        )
        .limit(1)
    )
    identifier = (
        previous.chain_id
        if previous
        else "paper-rejections:" + clock.now().astimezone(IST).date().isoformat()
    )
    records = list(
        await session.scalars(
            sa.select(AuditEvent)
            .where(
                AuditEvent.chain_id == identifier,
            )
            .order_by(AuditEvent.sequence)
            .with_for_update()
        )
    )
    if not verify_records(records) or any(
        _utc(record.occurred_at) > clock.utcnow() for record in records
    ):
        raise ValueError("broker rejection evidence integrity failure")
    if previous:
        return
    orders = [record.order_id for record in records] + [order.id]
    reached = len(orders) >= limit
    event = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=identifier,
            event_type="BROKER_REJECTION_OBSERVED",
            actor="paper_rejection_guard",
            mode=TradingMode.PAPER,
            severity="CRITICAL" if reached else "WARNING",
        ),
        {
            "order_id": order.id,
            "proposal_id": order.proposal_id,
            "result": {
                "source_audit_id": source.id,
                "order_ids": orders,
                "count": len(orders),
                "limit": limit,
                "limit_reached": reached,
            },
        },
        expected_count=len(records),
    )
    if reached:
        controls = EmergencyControls(clock)
        if not (await controls.restore_in_session(session))["entries_blocked"]:
            await controls.activate(
                "DISABLE_ENTRIES",
                actor="paper_rejection_guard",
                reason=f"PAPER_BROKER_REJECTION_LIMIT: {len(orders)}/{limit}; audit={event.id}",
                existing_session=session,
            )
        await enqueue(
            session,
            key="rejection-limit:" + event.id,
            event_type="BROKER_REJECTION_LIMIT",
            severity="CRITICAL",
            message=(
                f"PAPER broker rejection limit reached; entries disabled. audit={event.id}. "
                "Safe exits remain permitted."
            ),
            clock=clock,
            source_event=event,
        )
