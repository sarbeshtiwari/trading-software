"""FUND-006: explicit coverage and versioned corporate-event evidence feeding blackouts."""

from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.analysis.regime.events import EventCalendar, ScheduledEvent
from app.core.clock import UTC, Clock, get_clock
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.fundamental_versions import CorporateCalendarVersion
from app.db.models.instrument import Instrument
from app.fno.chain.model import aware


class CorporateEventEvidence(EvidenceModel):
    id: str = Field(min_length=1)
    kind: Literal[
        "RESULTS", "DIVIDEND", "SPLIT", "BONUS", "BOARD_MEETING", "AGM", "BUYBACK", "OTHER"
    ]
    at: AwareDatetime


class CorporateCalendarEvidence(EvidenceModel):
    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    coverage_start: AwareDatetime
    coverage_end: AwareDatetime
    events: tuple[CorporateEventEvidence, ...]

    @model_validator(mode="after")
    def coverage(self):
        if self.coverage_start > self.coverage_end or len(
            {event.id for event in self.events}
        ) != len(self.events):
            raise ValueError("invalid calendar coverage or duplicate event")
        if any(not self.coverage_start <= event.at <= self.coverage_end for event in self.events):
            raise ValueError("event outside declared coverage")
        return self

    def upcoming(
        self, *, start: datetime, end: datetime, as_of: datetime
    ) -> tuple[CorporateEventEvidence, ...]:
        if aware(as_of) < self.known_at or aware(start) > aware(end):
            raise ValueError("future calendar or reversed event query")
        if start < self.coverage_start or end > self.coverage_end:
            raise ValueError("calendar coverage unavailable")
        return tuple(
            sorted(
                (event for event in self.events if start <= event.at <= end),
                key=lambda event: (event.at, event.id),
            )
        )

    def blackouts(self, *, before: timedelta, after: timedelta) -> EventCalendar:
        if before < timedelta(0) or after < timedelta(0):
            raise ValueError("negative event blackout window")
        start, end = self.coverage_start + after, self.coverage_end - before
        if start > end:
            raise ValueError("blackout window exceeds calendar coverage")
        return EventCalendar(
            source=self.source,
            known_at=self.known_at,
            coverage_start=start,
            coverage_end=end,
            events=tuple(
                ScheduledEvent(
                    id=event.id,
                    source=self.source,
                    known_at=self.known_at,
                    start=event.at - before,
                    end=event.at + after,
                    high_impact=event.kind in ("RESULTS", "BOARD_MEETING"),
                    underlyings=(self.symbol,),
                )
                for event in self.events
            ),
        )


class CorporateCalendarStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or get_clock()

    async def import_json(self, content: str) -> str:
        evidence = CorporateCalendarEvidence.model_validate_json(content)
        async with db_session.session_scope() as session:
            return await self.import_in_session(session, evidence)

    async def import_in_session(self, session, evidence: CorporateCalendarEvidence) -> str:
        evidence = CorporateCalendarEvidence.model_validate(evidence.model_dump())
        received = self.clock.utcnow().astimezone(UTC)
        if evidence.known_at > received:
            raise ValueError("future corporate calendar")
        payload = evidence.model_dump(mode="json")
        instrument = await session.get(Instrument, evidence.instrument_id)
        if instrument is None or instrument.trading_symbol != evidence.symbol:
            raise ValueError("unknown or mismatched calendar instrument")
        existing = await session.scalar(
            sa.select(CorporateCalendarVersion).where(
                CorporateCalendarVersion.instrument_id == evidence.instrument_id,
                CorporateCalendarVersion.source == evidence.source,
                CorporateCalendarVersion.known_at == evidence.known_at.astimezone(UTC),
            )
        )
        if existing:
            if existing.payload != payload:
                raise ValueError("conflicting calendar revision")
            return existing.id
        record_id = new_id("ccv")
        session.add(
            CorporateCalendarVersion(
                id=record_id,
                instrument_id=evidence.instrument_id,
                source=evidence.source,
                known_at=evidence.known_at.astimezone(UTC),
                received_at=received,
                payload=payload,
            )
        )
        await session.flush()
        return record_id

    async def at(
        self, instrument_id: str, *, source: str, as_of: datetime
    ) -> CorporateCalendarEvidence | None:
        cutoff = aware(as_of).astimezone(UTC)
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(CorporateCalendarVersion)
                .where(
                    CorporateCalendarVersion.instrument_id == instrument_id,
                    CorporateCalendarVersion.source == source,
                    CorporateCalendarVersion.known_at <= cutoff,
                    CorporateCalendarVersion.received_at <= cutoff,
                )
                .order_by(CorporateCalendarVersion.known_at.desc())
                .limit(1)
            )
        return CorporateCalendarEvidence.model_validate(row.payload) if row else None
