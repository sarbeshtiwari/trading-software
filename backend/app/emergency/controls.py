"""Durable PAPER entry inhibition; activation never disables safe exits."""

from contextlib import asynccontextmanager

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.enums import HealthStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.system import SINGLETON_ID, SystemState
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.notifications.outbox import enqueue
from app.risk.emergency_state import emergency_state


@asynccontextmanager
async def control_session(existing):
    if existing is not None:
        yield existing
    else:
        async with db_session.session_scope() as session:
            yield session


class EmergencyControls:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def activate(self, action, *, actor, reason, existing_session=None):
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if action not in {"KILL", "DISABLE_ENTRIES", "FLATTEN"}:
            raise ValueError("unsupported emergency action")
        if not actor or len(reason.strip()) < 10:
            raise SafetyError("EMERGENCY_ACTOR_REASON_REQUIRED")
        get_trading_gate().block("emergency", "PAPER_EMERGENCY_CONTROL_ACTIVE")
        async with control_session(existing_session) as session:
            state = await session.get(SystemState, SINGLETON_ID, with_for_update=True)
            if state is None:
                state = SystemState(id=SINGLETON_ID, mode=TradingMode.PAPER)
                session.add(state)
            if state.mode != TradingMode.PAPER:
                raise SafetyError("SYSTEM_MODE_MISMATCH")
            await self.restore_in_session(session)
            state.new_entries_blocked = True
            state.new_entries_blocked_reason = reason
            if action == "KILL":
                state.kill_switch_active = True
                state.kill_switch_reason = reason
                state.kill_switch_actor = actor
                state.kill_switch_activated_at = self.clock.utcnow()
            await session.flush()
            event = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id="paper-emergency",
                    event_type="EMERGENCY_ACTIVATED",
                    actor=actor,
                    mode=TradingMode.PAPER,
                ),
                {
                    "result": {
                        "action": action,
                        "reason": reason,
                        "entries_blocked": True,
                        "kill_switch": state.kill_switch_active,
                    }
                },
            )
            await enqueue(
                session,
                key=event.id,
                event_type="KILL_SWITCH_ACTIVATED"
                if action == "KILL"
                else "PAPER_ENTRIES_DISABLED",
                severity="CRITICAL" if action == "KILL" else "WARNING",
                message=(
                    f"PAPER {action}: new entries inhibited. "
                    "Existing positions require monitoring; no closure implied."
                ),
                clock=self.clock,
                source_event=event,
            )
        return {"entries_blocked": True, "kill_switch": state.kill_switch_active}

    async def restore(self):
        try:
            async with db_session.session_scope() as session:
                return await self.restore_in_session(session)
        except Exception:
            get_trading_gate().block("emergency", "EMERGENCY_STATE_UNAVAILABLE")
            raise

    async def restore_in_session(self, session):
        return await emergency_state(session)

    async def clear(self, worker, *, actor, reason):
        if not actor or len(reason.strip()) < 10:
            raise SafetyError("EMERGENCY_ACTOR_REASON_REQUIRED")
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if worker is None or not worker.running or not worker.executor.ready:
            raise SafetyError("RUNNING_RECOVERED_WORKER_REQUIRED")
        async with worker.cycle_lock:
            report = await get_health_registry().run_all()
            if not any(result.critical for result in report.results) or any(
                result.critical and result.status != HealthStatus.PASS for result in report.results
            ):
                raise SafetyError("EMERGENCY_CLEAR_HEALTH_CHECKS_FAILED")
            await worker.executor._reconcile()
            async with db_session.session_scope() as session:
                await self.restore_in_session(session)
                state = await session.get(SystemState, SINGLETON_ID, with_for_update=True)
                if state is None or state.mode != TradingMode.PAPER:
                    raise SafetyError("SYSTEM_MODE_MISMATCH")
                state.new_entries_blocked = False
                state.new_entries_blocked_reason = None
                state.kill_switch_active = False
                state.kill_switch_reason = None
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id="paper-emergency",
                        event_type="EMERGENCY_CLEARED",
                        actor=actor,
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "result": {
                            "reason": reason,
                            "entries_blocked": False,
                            "kill_switch": False,
                            "health": report.to_dict(),
                        }
                    },
                )
            get_trading_gate().clear("emergency")
        return {"entries_blocked": False, "kill_switch": False}
