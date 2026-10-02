"""Expiry admission and durable position warnings use supplied contract dates."""

from datetime import time
from hashlib import sha256

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST, UTC
from app.core.enums import Segment, Severity
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.trading import Position
from app.modes import TradingMode
from app.notifications.outbox import enqueue


def expiry_blocker(instrument, now, cutoff):
    if now.utcoffset() is None:
        raise SafetyError("EXPIRY_CLOCK_REQUIRES_TIMEZONE")
    if instrument.segment != Segment.FNO:
        return None
    if instrument.expiry_date is None:
        return "FNO_EXPIRY_UNAVAILABLE"
    local = now.astimezone(IST)
    if instrument.expiry_date < local.date():
        return "FNO_CONTRACT_EXPIRED"
    if instrument.expiry_date == local.date() and local.time() >= time.fromisoformat(cutoff):
        return "FNO_EXPIRY_ENTRY_CUTOFF"
    return None


async def warn_expiring_positions(executor):
    async with db_session.session_scope() as session:
        rows = (
            await session.execute(
                sa.select(Position, Instrument)
                .join(Instrument, Instrument.id == Position.instrument_id)
                .where(
                    Position.mode == TradingMode.PAPER,
                    Position.net_quantity != 0,
                    Position.opened_at <= executor.clock.now().astimezone(UTC),
                    Instrument.segment == Segment.FNO,
                )
            )
        ).all()
        for position, instrument in rows:
            reason = expiry_blocker(
                instrument, executor.clock.now(), executor.settings.fno_expiry_entry_cutoff_time
            )
            if reason is None:
                continue
            identity = f"{position.id}:{instrument.expiry_date}:{reason}"
            chain = "exp" + sha256(identity.encode()).hexdigest()[:32]
            existing = await session.scalar(
                sa.select(AuditEvent.id).where(AuditEvent.chain_id == chain)
            )
            if existing:
                if not await AuditService(executor.clock).verify(chain, expected_count=1):
                    raise SafetyError("EXPIRY_WARNING_INTEGRITY_FAILURE")
                continue
            event = await AuditService(executor.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=chain,
                    event_type="FNO_EXPIRY_ACTION_REQUIRED",
                    actor="paper_expiry_monitor",
                    mode=TradingMode.PAPER,
                    severity=Severity.CRITICAL,
                ),
                {
                    "position_id": position.id,
                    "instrument_id": instrument.id,
                    "result": {
                        "reason": reason,
                        "quantity": position.net_quantity,
                        "expiry": instrument.expiry_date.isoformat()
                        if instrument.expiry_date
                        else None,
                        "cutoff": executor.settings.fno_expiry_entry_cutoff_time,
                    },
                },
                expected_count=0,
            )
            await enqueue(
                session,
                key=chain,
                event_type="FNO_EXPIRY_ACTION_REQUIRED",
                severity=Severity.CRITICAL,
                clock=executor.clock,
                source_event=event,
                message=(
                    f"PAPER position {position.id}: {reason}; quantity {position.net_quantity}. "
                    "Owner action required; no settlement or exit success assumed."
                ),
            )
