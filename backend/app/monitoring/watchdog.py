"""Periodic health enforcement and transactional transition notifications."""

import asyncio
import logging

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.enums import HealthStatus, Severity
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import current_mode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthCheck, get_health_registry
from app.notifications.outbox import enqueue

logger = logging.getLogger(__name__)


class UnavailableNewsCheck(HealthCheck):
    name = "news"
    critical = False

    async def run(self):
        return HealthStatus.DEGRADED, "NEWS SERVICE DEGRADED: no verified news service", {}


class HealthWatchdog:
    def __init__(self, interval_seconds, *, registry=None, gate=None, required=None, clock=None):
        if interval_seconds <= 0:
            raise ValueError("Health interval must be positive")
        self.interval = interval_seconds
        self.registry = registry or get_health_registry()
        if "news" not in self.registry.names:
            self.registry.register(UnavailableNewsCheck())
        self.gate = gate or get_trading_gate()
        self.clock = clock or get_clock()
        self.required = frozenset(
            required
            if required is not None
            else (
                "database",
                "redis",
                "groww_auth",
                "market_data",
                "account",
                "margin",
                "positions",
                "order_service",
                "instruments",
                "risk_config",
                "system_clock",
                "market_status",
                "mode",
            )
        )
        self._last = None
        self._stop = asyncio.Event()
        self._task = None
        self._lock = asyncio.Lock()

    async def cycle(self):
        async with self._lock:
            try:
                report = await self.registry.run_all()
                summary = {
                    result.name: {"status": result.status.value, "critical": result.critical}
                    for result in report.results
                }
                missing = sorted(self.required - summary.keys())
                blocked = sorted(
                    result.name
                    for result in report.results
                    if result.critical and result.status is not HealthStatus.PASS
                )
                if blocked or missing:
                    self.gate.block(
                        "health_watchdog", "HEALTH UNAVAILABLE: " + ", ".join(blocked + missing)
                    )
                state = {"checks": summary, "missing": missing, "blocked": blocked}
                if state != self._last:
                    await asyncio.wait_for(self._persist(state), timeout=10)
                    self._last = state
                if not blocked and not missing:
                    self.gate.clear("health_watchdog")
                    self.gate.clear("health")
                self.gate.clear("health_watchdog_error")
                return report
            except Exception as error:
                self.gate.block("health_watchdog_error", "HEALTH MONITOR OR AUDIT UNAVAILABLE")
                logger.error("Health watchdog failed", extra={"error_type": type(error).__name__})
                return None

    async def _persist(self, state):
        degraded = any(check["status"] != "PASS" for check in state["checks"].values())
        critical = bool(state["blocked"] or state["missing"])
        severity = (
            Severity.CRITICAL if critical else Severity.WARNING if degraded else Severity.INFO
        )
        message = "TRADING DISABLED — HEALTH UNAVAILABLE" if critical else "HEALTH CHECKS RECOVERED"
        if critical:
            message += ": " + ", ".join(state["blocked"] + state["missing"])
        if not critical and degraded:
            message = "NON-CRITICAL SERVICES DEGRADED"
        news = state["checks"].get("news")
        if news is None or news["status"] != "PASS":
            message += "; NEWS SERVICE DEGRADED"
        async with db_session.session_scope() as session:
            chain_id = "health:" + current_mode().value
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == chain_id)
                        .order_by(AuditEvent.sequence)
                        .with_for_update()
                    )
                ).all()
            )
            if not verify_records(records):
                raise ValueError("health transition audit integrity failure")
            previous = records[-1].result if records else None
            if previous == state:
                return
            record = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=chain_id,
                    event_type="HEALTH_TRANSITION",
                    actor="health_watchdog",
                    mode=current_mode(),
                    severity=severity,
                ),
                {"result": state},
                expected_count=len(records),
            )
            await enqueue(
                session,
                key="health:" + record.id,
                event_type="HEALTH_TRANSITION",
                severity=severity,
                message=message,
                clock=self.clock,
                source_event=record,
            )
            await self._provider_notices(session, records, state, record)

    async def _provider_notices(self, session, records, state, record):
        for name, kind in (("groww_auth", "AUTH_FAILURE"), ("market_data", "FEED_OUTAGE")):
            before = next(
                (
                    status
                    for previous in reversed(records)
                    if (status := previous.result.get("checks", {}).get(name, {}).get("status"))
                    in {"PASS", "FAIL"}
                ),
                None,
            )
            after = state["checks"].get(name, {}).get("status")
            if before == after:
                continue
            if after == "FAIL" or (before == "FAIL" and after == "PASS"):
                recovered = after == "PASS"
                await enqueue(
                    session,
                    key=f"health:{record.id}:{name}",
                    event_type=kind,
                    severity="INFO" if recovered else "CRITICAL",
                    message=f"{name} health check {'RECOVERED' if recovered else 'FAILED'}; "
                    f"mode={current_mode().value}; audit={record.id}. "
                    "This is provider health evidence, not live-order verification.",
                    clock=self.clock,
                    source_event=record,
                )

    async def start(self):
        if self._task is not None:
            raise RuntimeError("Health watchdog already started")
        self.gate.block("health_watchdog", "HEALTH CHECKS PENDING")
        await self.cycle()
        self._task = asyncio.create_task(self._run(), name="health-watchdog")

    async def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    await self.cycle()
        finally:
            self.gate.block("health_watchdog", "HEALTH WATCHDOG STOPPED")

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await self._task
