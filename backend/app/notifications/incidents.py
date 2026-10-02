"""Durable PAPER incident transitions and transactional notification requests."""

import hashlib

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.notifications.outbox import enqueue


class PaperIncidents:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def observe(self, kind, *, active):
        try:
            async with db_session.session_scope() as session:
                await self.observe_in_session(session, kind, active=active)
            get_trading_gate().clear("paper_incident_audit")
        except Exception:
            get_trading_gate().block("paper_incident_audit", "PAPER_INCIDENT_AUDIT_UNAVAILABLE")
            raise

    async def observe_in_session(self, session, kind, *, active):
        if kind not in {"RECONCILIATION_DISCREPANCY", "WORKER_FAILURE", "FEED_OUTAGE"}:
            raise ValueError("unsupported PAPER incident")
        identifier = "inc" + hashlib.sha256(("PAPER:" + kind).encode()).hexdigest()[:37]
        records = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == identifier)
                    .order_by(AuditEvent.sequence)
                    .with_for_update()
                )
            ).all()
        )
        if not verify_records(records):
            raise ValueError("incident audit integrity failure")
        if (not records and not active) or (records and records[-1].result["active"] == active):
            return
        severity = "CRITICAL" if active else "INFO"
        event = await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier,
                event_type="PAPER_INCIDENT_TRANSITION",
                actor="paper_incidents",
                mode=TradingMode.PAPER,
                severity=severity,
            ),
            {"result": {"kind": kind, "active": active, "execution_realism": "SIMULATED"}},
            expected_count=len(records),
        )
        await enqueue(
            session,
            key="incident:" + event.id,
            event_type=kind,
            severity=severity,
            message=f"PAPER SIMULATED {kind}: {'ACTIVE' if active else 'RECOVERED'}; "
            f"audit={event.id}; other trading gates remain authoritative.",
            clock=self.clock,
            source_event=event,
        )
