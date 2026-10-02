"""Authenticated runtime evidence, without probing or enabling broker execution."""

from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel

from app.audit.service import AuditService
from app.config import get_settings
from app.core.calendar import get_trading_calendar
from app.core.clock import UTC, get_clock
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.instrument_snapshot import InstrumentMasterSnapshot
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.trading.worker import worker_status

router = APIRouter(prefix="/system", tags=["system"])


class RuntimeReadinessView(BaseModel):
    generated_at: datetime
    trading_mode: str
    execution_worker_enabled: bool
    worker_state: str
    entry_gate_open: bool
    blockers: list[str]
    health_state: str
    health_checked_at: datetime | None
    calendar_warning: str | None
    instrument_count: int
    restricted_instrument_count: int
    instrument_snapshot_id: str | None
    instrument_snapshot_received_at: datetime | None
    instrument_snapshot_source: str | None
    instrument_snapshot_audit_verified: bool | None
    groww_live_execution: Literal["UNVERIFIED"] = "UNVERIFIED"


@router.get("/readiness", response_model=RuntimeReadinessView)
async def readiness():
    settings = get_settings()
    now = get_clock().utcnow()
    report = get_health_registry().last_report
    state = "UNAVAILABLE"
    if report:
        age = now - report.generated_at
        state = (
            report.overall_status.value
            if timedelta(0) <= age <= timedelta(seconds=settings.healthcheck_interval_seconds * 2)
            else "STALE"
        )
    async with db_session.session_scope() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Instrument))
        restricted = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Instrument)
            .where(Instrument.is_restricted.is_(True))
        )
        snapshot = (
            await session.execute(
                sa.select(
                    InstrumentMasterSnapshot.id,
                    InstrumentMasterSnapshot.received_at,
                    InstrumentMasterSnapshot.source,
                )
                .where(InstrumentMasterSnapshot.received_at <= now)
                .order_by(InstrumentMasterSnapshot.received_at.desc())
                .limit(1)
            )
        ).first()
    received = snapshot.received_at if snapshot else None
    if received is not None and received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return RuntimeReadinessView(
        generated_at=now,
        trading_mode=settings.trading_mode.value,
        execution_worker_enabled=settings.paper_worker_enabled,
        worker_state=worker_status()["status"],
        entry_gate_open=get_trading_gate().new_entries_allowed,
        blockers=list(get_trading_gate().state.reasons),
        health_state=state,
        health_checked_at=report.generated_at if report else None,
        calendar_warning=get_trading_calendar().completeness_warning(get_clock().now().year),
        instrument_count=count,
        restricted_instrument_count=restricted,
        instrument_snapshot_id=snapshot.id if snapshot else None,
        instrument_snapshot_received_at=received,
        instrument_snapshot_source=snapshot.source if snapshot else None,
        instrument_snapshot_audit_verified=await AuditService().verify(snapshot.id)
        if snapshot
        else None,
    )
