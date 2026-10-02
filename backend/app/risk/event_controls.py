"""Audited owner-published calendars veto entries independently of strategy flags."""

from datetime import timedelta
from hashlib import sha256
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.analysis.fundamental.calendar import CorporateCalendarEvidence, CorporateCalendarStore
from app.analysis.regime.events import EventCalendar
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError
from app.db.models.audit import AuditEvent
from app.db.models.fundamental_versions import CorporateCalendarVersion
from app.db.models.instrument import Instrument
from app.modes import TradingMode
from app.news.entities import aware, history


class EventControlError(SafetyError):
    pass


class EventControlChange(EvidenceModel):
    origin: DataOrigin
    enabled: bool = Field(strict=True)
    calendars: tuple[EventCalendar, ...] = Field(default=(), max_length=20)
    corporate: CorporateCalendarEvidence | None = None
    before_seconds: int | None = Field(default=None, ge=0, le=31536000, strict=True)
    after_seconds: int | None = Field(default=None, ge=0, le=31536000, strict=True)
    max_age_seconds: int = Field(ge=1, le=31536000, strict=True)
    restricted_strategies: tuple[str, ...] = Field(default=(), max_length=100)
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str

    @model_validator(mode="after")
    def coherent(self):
        if self.enabled and not self.calendars and self.corporate is None:
            raise ValueError("Enabled event controls require declared calendar evidence")
        if self.corporate is not None and (
            self.before_seconds is None or self.after_seconds is None
        ):
            raise ValueError("Explicit corporate blackout windows required")
        if self.corporate is None and (
            self.before_seconds is not None or self.after_seconds is not None
        ):
            raise ValueError("Corporate windows require corporate evidence")
        if len(set(self.restricted_strategies)) != len(self.restricted_strategies) or any(
            not name.strip() or len(name) > 64 for name in self.restricted_strategies
        ):
            raise ValueError("Invalid restricted strategy scope")
        calendars = compiled(self)
        if (
            len({item.source for item in calendars}) != len(calendars)
            or sum(len(item.events) for item in calendars) > 1000
        ):
            raise ValueError("Duplicate calendar source or event scope exceeded")
        return self


class EventControlState(EvidenceModel):
    instrument_id: str
    origin: DataOrigin
    as_of: AwareDatetime
    event_id: str | None = None
    known_at: AwareDatetime | None = None
    status: Literal[
        "UNCONFIGURED", "DISABLED", "CLEAR", "BLACKOUT", "UNAVAILABLE", "NOT_APPLICABLE"
    ]
    event_ids: tuple[str, ...] = ()
    restricted_strategies: tuple[str, ...] = ()
    calendars: tuple[EventCalendar, ...] = ()
    corporate_record_id: str | None = None
    control: EventControlChange | None = None
    verification: Literal["OWNER_PUBLISHED_NOT_EXTERNALLY_VERIFIED"] = (
        "OWNER_PUBLISHED_NOT_EXTERNALLY_VERIFIED"
    )


def compiled(change):
    calendars = list(change.calendars)
    if change.corporate is not None:
        calendars.append(
            change.corporate.blackouts(
                before=timedelta(seconds=change.before_seconds),
                after=timedelta(seconds=change.after_seconds),
            )
        )
    return tuple(calendars)


def chain_id(instrument_id, origin):
    return (
        "evc_"
        + sha256(f"PAPER:{instrument_id}:{DataOrigin(origin).value}".encode()).hexdigest()[:36]
    )


async def state_at(session, instrument_id, origin, *, as_of=None, strategy_id=None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff.utcoffset() is None or cutoff > get_clock().utcnow():
        raise ValueError("Aware nonfuture event control cutoff required")
    result = EventControlState(
        instrument_id=instrument_id, origin=origin, as_of=cutoff, status="UNCONFIGURED"
    )
    records = await history(session, chain_id(instrument_id, origin), cutoff if as_of else None)
    if records and any(aware(record.occurred_at) > cutoff for record in records):
        raise ValueError("Event control clock regression")
    prior = None
    for record in records:
        change = EventControlChange.model_validate(record.data_used)
        if (
            record.event_type != "EVENT_CONTROL_PUBLISHED"
            or record.mode != TradingMode.PAPER
            or record.instrument_id != instrument_id
            or change.origin != DataOrigin(origin)
            or change.expected_event_id != (prior.id if prior else None)
            or (prior and aware(record.occurred_at) < aware(prior.occurred_at))
        ):
            raise ValueError("Event control identity or chronology mismatch")
        prior = record
    if not records:
        return result
    record = records[-1]
    calendars = compiled(change)
    if [calendar.model_dump(mode="json") for calendar in calendars] != record.result["calendars"]:
        raise ValueError("Event control snapshot mismatch")
    if any(calendar.known_at > aware(record.occurred_at) for calendar in calendars):
        raise ValueError("Event calendar contains future knowledge")
    corporate_id = record.result["corporate_record_id"]
    if bool(corporate_id) != (change.corporate is not None):
        raise ValueError("Corporate calendar reference mismatch")
    if corporate_id:
        corporate = await session.get(CorporateCalendarVersion, corporate_id)
        if (
            corporate is None
            or change.corporate is None
            or corporate.instrument_id != instrument_id
            or corporate.source != change.corporate.source
            or aware(corporate.known_at) != change.corporate.known_at
            or corporate.payload != change.corporate.model_dump(mode="json")
            or aware(corporate.received_at) > aware(record.occurred_at)
        ):
            raise ValueError("Corporate calendar record unavailable or changed")
    result = result.model_copy(
        update={
            "event_id": record.id,
            "known_at": aware(record.occurred_at),
            "calendars": calendars,
            "corporate_record_id": corporate_id,
            "control": change,
            "restricted_strategies": change.restricted_strategies,
        }
    )
    return evaluate_state(result, await session.get(Instrument, instrument_id), strategy_id)


def evaluate_state(result, instrument, strategy_id):
    change = result.control
    cutoff = result.as_of
    if not change.enabled:
        return result.model_copy(update={"status": "DISABLED"})
    if (
        strategy_id is not None
        and change.restricted_strategies
        and strategy_id not in change.restricted_strategies
    ):
        return result.model_copy(update={"status": "NOT_APPLICABLE"})
    if instrument is None:
        raise ValueError("Event control instrument unavailable")
    active = set()
    for calendar in result.calendars:
        if cutoff - calendar.known_at > timedelta(seconds=change.max_age_seconds):
            return result.model_copy(update={"status": "UNAVAILABLE"})
        for symbol in {instrument.trading_symbol, instrument.key, instrument.underlying} - {None}:
            events = calendar.active(symbol, cutoff)
            if events is None:
                return result.model_copy(update={"status": "UNAVAILABLE"})
            active.update(f"{calendar.source}:{identifier}" for identifier in events)
    return result.model_copy(
        update={"status": "BLACKOUT" if active else "CLEAR", "event_ids": tuple(sorted(active))}
    )


async def publish(session, instrument_id, change, *, actor):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise ValueError("Event controls are PAPER-only")
    change = EventControlChange.model_validate(change.model_dump())
    phrase = "PUBLISH PAPER EVENT CONTROL" if change.enabled else "DISABLE PAPER EVENT CONTROL"
    if change.confirmation != phrase or len(change.reason.strip()) < 10:
        raise ValueError("Explicit event control review required")
    instrument = await session.get(Instrument, instrument_id)
    if instrument is None or (change.corporate and change.corporate.instrument_id != instrument_id):
        raise ValueError("Unknown or mismatched event instrument")
    records = await history(session, chain_id(instrument_id, change.origin))
    if (records[-1].id if records else None) != change.expected_event_id or (
        records and aware(records[-1].occurred_at) > get_clock().utcnow()
    ):
        raise ValueError("Event control changed or clock regressed")
    calendars = compiled(change)
    if any(calendar.known_at > get_clock().utcnow() for calendar in calendars):
        raise ValueError("Future calendar knowledge")
    corporate_id = (
        await CorporateCalendarStore().import_in_session(session, change.corporate)
        if change.corporate
        else None
    )
    await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(instrument_id, change.origin),
            event_type="EVENT_CONTROL_PUBLISHED",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "instrument_id": instrument_id,
            "data_used": change.model_dump(mode="json"),
            "result": {
                "calendars": [calendar.model_dump(mode="json") for calendar in calendars],
                "corporate_record_id": corporate_id,
            },
        },
        expected_count=len(records),
    )
    return await state_at(session, instrument_id, change.origin)


async def list_states(session):
    records = (
        await session.scalars(
            sa.select(AuditEvent).where(
                AuditEvent.event_type == "EVENT_CONTROL_PUBLISHED",
                AuditEvent.mode == TradingMode.PAPER,
                AuditEvent.occurred_at <= get_clock().utcnow(),
            )
        )
    ).all()
    identities = {(record.instrument_id, record.data_used["origin"]) for record in records}
    return [
        await state_at(session, instrument, origin) for instrument, origin in sorted(identities)
    ]


async def require_event_entries(session, market, *, strategy_id=None):
    current = await state_at(session, market.instrument_id, market.data_origin)
    if market.mode != TradingMode.PAPER or current.status in ("UNCONFIGURED", "DISABLED"):
        return current
    if current.restricted_strategies:
        if strategy_id is None:
            raise EventControlError(
                "EVENT_STRATEGY_SCOPE_REQUIRED",
                code="EVENT_STRATEGY_SCOPE_REQUIRED",
                context=current.model_dump(mode="json"),
            )
        if strategy_id not in current.restricted_strategies:
            return current.model_copy(update={"status": "NOT_APPLICABLE"})
    if current.status in ("BLACKOUT", "UNAVAILABLE"):
        code = "EVENT_BLACKOUT" if current.status == "BLACKOUT" else "EVENT_CALENDAR_UNAVAILABLE"
        raise EventControlError(code, code=code, context=current.model_dump(mode="json"))
    return current
