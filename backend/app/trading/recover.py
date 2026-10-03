"""Explicit recovery restores execution supervision without authorizing entries."""

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate


async def recover_worker(worker, *, actor, reason):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise SafetyError("PAPER_MODE_REQUIRED")
    if worker is None or not worker.running:
        raise SafetyError("RUNNING_WORKER_REQUIRED")
    if not actor or len(reason.strip()) < 10:
        raise SafetyError("RECOVERY_ACTOR_REASON_REQUIRED")
    async with worker.cycle_lock:
        if not worker.running:
            raise SafetyError("RUNNING_WORKER_REQUIRED")
        await EmergencyControls(worker.clock).activate(
            "DISABLE_ENTRIES", actor=actor, reason=reason
        )
        worker.failed = True
        worker.executor.ready = False
        gate = get_trading_gate()
        gate.block("paper_worker_error", "PAPER_WORKER_REVIEW_REQUIRED")
        try:
            await worker.executor.recover()
            await worker.executor.verify_protection()
            worker.detail = "Owner recovered supervision; entries disabled; review still required"
            await worker._heartbeat(beat_at=worker.clock.utcnow())
            async with db_session.session_scope() as session:
                await AuditService(worker.clock).append_in_session(
                    session, AuditIdentity(
                        chain_id=new_id("rcv"), event_type="PAPER_WORKER_RECOVERED",
                        actor=actor, mode=TradingMode.PAPER,
                    ), {"result": {
                        "reason": reason.strip(), "entries_authorized": False,
                        "continuous_protection_verified": False,
                        "worker_review_required": True,
                    }},
                )
            gate.clear("paper_storage")
        except BaseException:
            worker.executor.ready = False
            worker.detail = "Recovery incomplete; entries remain disabled"
            gate.block("paper_storage", "PAPER_RECOVERY_INCOMPLETE")
            raise
