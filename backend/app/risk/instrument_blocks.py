"""Owner instrument vetoes and current catalog restrictions, never exchange verification."""

from hashlib import sha256
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.errors import SafetyError
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.fno.restrictions import require_entries as require_fno_entries
from app.modes import TradingMode
from app.news.entities import aware, history
from app.notifications.outbox import enqueue


class InstrumentEntryError(SafetyError):
    pass


class InstrumentBlockState(EvidenceModel):
    instrument_id: str
    mode: Literal["PAPER"] = "PAPER"
    scope: Literal["ALL_PAPER_DATA_ORIGINS"] = "ALL_PAPER_DATA_ORIGINS"
    manually_blocked: bool = False
    event_id: str | None = None
    known_at: AwareDatetime | None = None
    reason: str | None = None
    actor: str | None = None
    catalog_restricted: bool
    catalog_active: bool
    catalog_reason: str | None
    catalog_source: str | None
    catalog_known_at: AwareDatetime
    catalog_evidence: Literal["CURRENT_CATALOG_NOT_VERIFIED_EXCHANGE_BAN"] = (
        "CURRENT_CATALOG_NOT_VERIFIED_EXCHANGE_BAN"
    )


class InstrumentBlockChange(EvidenceModel):
    blocked: bool = Field(strict=True)
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


def chain_id(instrument_id):
    return "imb_" + sha256(f"PAPER:{instrument_id}".encode()).hexdigest()[:36]


async def state(session, instrument_id):
    instrument = await session.get(Instrument, instrument_id)
    if instrument is None:
        raise ValueError("Instrument unavailable")
    known = max(aware(instrument.created_at), aware(instrument.updated_at or instrument.created_at))
    if known > get_clock().utcnow():
        raise ValueError("Current catalog evidence is future dated")
    result = InstrumentBlockState(
        instrument_id=instrument_id,
        catalog_restricted=instrument.is_restricted,
        catalog_active=instrument.is_active,
        catalog_reason=instrument.restriction_reason,
        catalog_source=instrument.source,
        catalog_known_at=known,
    )
    records = await history(session, chain_id(instrument_id))
    for record in records:
        if (
            record.event_type != "MANUAL_INSTRUMENT_BLOCK"
            or record.mode != TradingMode.PAPER
            or record.instrument_id != instrument_id
            or aware(record.occurred_at) > get_clock().utcnow()
            or (result.known_at and aware(record.occurred_at) < result.known_at)
        ):
            raise ValueError("Manual block evidence identity or clock mismatch")
        change = InstrumentBlockChange.model_validate(record.result["change"])
        if change.expected_event_id != result.event_id or change.blocked == result.manually_blocked:
            raise ValueError("Manual block transition mismatch")
        result = result.model_copy(
            update={
                "manually_blocked": change.blocked,
                "event_id": record.id,
                "known_at": aware(record.occurred_at),
                "reason": change.reason,
                "actor": record.actor,
            }
        )
    return result


async def configure(session, instrument_id, change, *, actor):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise ValueError("Instrument controls are PAPER-only")
    change = InstrumentBlockChange.model_validate(change.model_dump())
    phrase = "BLOCK PAPER INSTRUMENT" if change.blocked else "UNBLOCK PAPER INSTRUMENT"
    if change.confirmation != phrase or len(change.reason.strip()) < 10:
        raise ValueError("Explicit instrument control confirmation and reason required")
    before = await state(session, instrument_id)
    if before.event_id != change.expected_event_id or before.manually_blocked == change.blocked:
        raise ValueError("Manual block changed or requested transition is unavailable")
    records = await history(session, chain_id(instrument_id))
    if (records[-1].id if records else None) != before.event_id:
        raise ValueError("Manual block changed during review")
    event = await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(instrument_id),
            event_type="MANUAL_INSTRUMENT_BLOCK",
            actor=actor,
            mode=TradingMode.PAPER,
            severity="CRITICAL" if change.blocked else "INFO",
        ),
        {
            "instrument_id": instrument_id,
            "data_used": before.model_dump(mode="json"),
            "result": {"change": change.model_dump(mode="json")},
        },
        expected_count=len(records),
    )
    if change.blocked:
        await enqueue(
            session,
            key=f"manual-block:{event.id}",
            event_type="MANUAL_INSTRUMENT_BLOCK",
            severity="CRITICAL",
            message=(
                f"PAPER entries manually blocked for {instrument_id}; audit={event.id}. "
                "Exits remain enabled."
            ),
            clock=get_clock(),
            source_event=event,
        )
    return await state(session, instrument_id)


async def list_states(session):
    identifiers = set(
        await session.scalars(
            sa.select(AuditEvent.instrument_id)
            .where(
                AuditEvent.event_type == "MANUAL_INSTRUMENT_BLOCK",
                AuditEvent.mode == TradingMode.PAPER,
            )
            .distinct()
        )
    )
    identifiers.update(
        await session.scalars(sa.select(Instrument.id).where(Instrument.is_restricted.is_(True)))
    )
    return [await state(session, identifier) for identifier in sorted(identifiers)]


async def require_instrument_entries(session, market):
    if market.mode != TradingMode.PAPER:
        return
    await require_fno_entries(session, market)
    if await session.get(Instrument, market.instrument_id) is None:
        return
    current = await state(session, market.instrument_id)
    if current.manually_blocked or current.catalog_restricted or not current.catalog_active:
        code = (
            "MANUALLY_BLOCKED"
            if current.manually_blocked
            else "INSTRUMENT_RESTRICTED"
            if current.catalog_restricted
            else "INSTRUMENT_INACTIVE"
        )
        raise InstrumentEntryError(code, code=code, context=current.model_dump(mode="json"))
