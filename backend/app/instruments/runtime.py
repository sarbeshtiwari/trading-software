"""Opt-in pre-open catalog refresh using the audited production loader."""

import asyncio
from datetime import time

import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.audit.service import AuditIdentity, AuditService
from app.brokers.groww.endpoints import Endpoints
from app.core.calendar import get_trading_calendar
from app.core.clock import IST, UTC, get_clock
from app.core.enums import HealthStatus
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument_snapshot import InstrumentMasterSnapshot
from app.instruments.loader import InstrumentLoader
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthCheck


class InstrumentRuntime:
    def __init__(self, settings, *, loader=None, clock=None, calendar=None):
        self.settings = settings
        self.clock = clock or get_clock()
        self.calendar = calendar or get_trading_calendar()
        self.loader = loader or InstrumentLoader(settings, clock=self.clock)
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self.lock = asyncio.Lock()
        self.status = "DISABLED"
        self.task = None
        self.running = False
        self.attempted_day = None

    async def start(self):
        if not self.settings.instrument_refresh_enabled:
            return
        if self.settings.trading_mode != TradingMode.PAPER:
            self.status = "UNSUPPORTED_MODE"
            return
        if self.running:
            raise RuntimeError("Instrument maintenance already started")
        self.running = True
        self.status = "PENDING"
        get_trading_gate().block("instrument_refresh", "INSTRUMENT_REFRESH_PENDING")
        self.scheduler.add_job(
            self.cycle,
            "interval",
            seconds=60,
            max_instances=1,
            coalesce=True,
            next_run_time=self.clock.utcnow(),
        )
        self.scheduler.start()

    async def cycle(self):
        if not self.running or self.lock.locked():
            return
        async with self.lock:
            self.task = asyncio.current_task()
            try:
                await self._refresh()
            except asyncio.CancelledError:
                self.status = "STOPPED"
                raise
            except Exception as error:
                self.status = "FAILED"
                try:
                    await AuditService(self.clock).append(
                        AuditIdentity(
                            chain_id=new_id("imf"),
                            event_type="INSTRUMENT_REFRESH_FAILED",
                            actor="instrument-scheduler",
                            mode=TradingMode.PAPER,
                        ),
                        {"result": {"error_type": type(error).__name__}},
                    )
                except Exception:
                    self.status = "AUDIT_UNAVAILABLE"
            finally:
                self.task = None
                if self.status == "CURRENT":
                    get_trading_gate().clear("instrument_refresh")
                else:
                    get_trading_gate().block("instrument_refresh", self.status)

    async def _refresh(self):
        now = self.clock.now().astimezone(IST)
        incomplete = self.calendar.completeness_warning(now.year)
        if incomplete or not self.calendar.is_trading_day(now.date()):
            self.status = "CALENDAR_UNVERIFIED" if incomplete else "MARKET_CLOSED"
            return
        async with db_session.session_scope() as session:
            if session.bind.dialect.name == "postgresql":
                acquired = await session.scalar(
                    sa.text("SELECT pg_try_advisory_xact_lock(78134017)")
                )
                if not acquired:
                    self.status = "OTHER_OWNER"
                    return
            latest = (
                await session.execute(
                    sa.select(InstrumentMasterSnapshot.id, InstrumentMasterSnapshot.received_at)
                    .where(InstrumentMasterSnapshot.source == Endpoints.INSTRUMENTS_CSV.resolve())
                    .order_by(InstrumentMasterSnapshot.received_at.desc())
                    .limit(1)
                )
            ).first()
            if latest:
                received = latest.received_at
                received = received.replace(tzinfo=UTC) if received.tzinfo is None else received
                if received > self.clock.utcnow():
                    raise ValueError("Future instrument snapshot")
                if received.astimezone(IST).date() == now.date():
                    if not await AuditService(self.clock).verify(latest.id):
                        raise ValueError("Instrument snapshot audit unavailable")
                    self.status = "CURRENT"
                    return
            special = self.calendar.special_session(now.date())
            cutoff = min(time(9), special.start) if special else time(9)
            if not time(8) <= now.time().replace(tzinfo=None) < cutoff:
                self.status = "AWAITING_PREOPEN" if now.hour < 8 else "REFRESH_MISSED"
                return
            if self.attempted_day == now.date():
                return
            failed_at = await session.scalar(
                sa.select(AuditEvent.occurred_at)
                .where(AuditEvent.event_type == "INSTRUMENT_REFRESH_FAILED")
                .order_by(AuditEvent.occurred_at.desc())
                .limit(1)
            )
            if failed_at is not None:
                failed_at = failed_at.replace(tzinfo=UTC) if failed_at.tzinfo is None else failed_at
                if failed_at.astimezone(IST).date() >= now.date():
                    self.status = "FAILED"
                    return
            self.attempted_day = now.date()
            self.status = "REFRESHING"
            await self.loader.load(quarantine_duplicates=True)
            self.status = "CURRENT"

    async def stop(self):
        self.running = False
        if self.scheduler.running:
            self.scheduler.pause()
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.status = "STOPPED"
        if self.settings.instrument_refresh_enabled:
            get_trading_gate().block("instrument_refresh", "INSTRUMENT_REFRESH_STOPPED")


class InstrumentRefreshCheck(HealthCheck):
    name = "instrument_refresh"
    critical = True

    def __init__(self, runtime):
        self.runtime = runtime

    async def run(self):
        state = self.runtime.status
        if not self.runtime.settings.instrument_refresh_enabled:
            return HealthStatus.SKIPPED, "Automatic instrument maintenance disabled", {}
        status = HealthStatus.PASS if state == "CURRENT" else HealthStatus.FAIL
        return status, state, {"automatic_enabled": True, "state": state}
