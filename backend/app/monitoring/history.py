"""Bounded, integrity-checked health observations; gaps are not continuous evidence."""

from datetime import timedelta

import sqlalchemy as sa
from pydantic import AwareDatetime, Field

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.core.clock import UTC
from app.core.enums import HealthStatus
from app.db.models.audit import AuditEvent

MAX_RECORDS = 10000


class HealthComponent(EvidenceModel):
    status: HealthStatus
    critical: bool = Field(strict=True)


class RecordedHealth(EvidenceModel):
    checks: dict[str, HealthComponent]
    missing: tuple[str, ...]
    blocked: tuple[str, ...]


class HealthObservation(EvidenceModel):
    audit_id: str
    sequence: int
    observed_at: AwareDatetime
    state: RecordedHealth


class HealthHistory(EvidenceModel):
    mode: str
    from_at: AwareDatetime
    to_at: AwareDatetime
    preceding: HealthObservation | None
    observations: tuple[HealthObservation, ...]
    has_more: bool
    next_offset: int | None
    scope: str = (
        "Recorded transitions only; intervals between observations are not verified health."
    )


class HistoryTooLarge(ValueError):
    pass


async def read_history(session, mode, from_at, to_at, now, *, offset=0, limit=50):
    if not from_at <= to_at <= now or to_at - from_at > timedelta(days=31):
        raise ValueError("invalid health history interval")
    records = list(await session.scalars(
        sa.select(AuditEvent).where(AuditEvent.chain_id == "health:" + mode.value)
        .order_by(AuditEvent.sequence).limit(MAX_RECORDS + 1)
    ))
    if len(records) > MAX_RECORDS:
        raise HistoryTooLarge("health history verification capacity exceeded")
    if not verify_records(records):
        raise ValueError("health audit integrity failure")
    observations = []
    preceding = None
    previous_time = None
    for record in records:
        observed = record.occurred_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if (record.event_type != "HEALTH_TRANSITION" or record.actor != "health_watchdog"
                or record.mode != mode or observed > now
                or (previous_time is not None and observed < previous_time)):
            raise ValueError("invalid health history evidence")
        previous_time = observed
        observation = HealthObservation(
            audit_id=record.id, sequence=record.sequence, observed_at=observed,
            state=RecordedHealth.model_validate(record.result),
        )
        if observed < from_at:
            preceding = observation
        elif observed <= to_at:
            observations.append(observation)
    has_more = len(observations) > offset + limit
    return HealthHistory(
        mode=mode.value, from_at=from_at, to_at=to_at, preceding=preceding,
        observations=tuple(observations[offset:offset + limit]), has_more=has_more,
        next_offset=offset + limit if has_more else None,
    )
