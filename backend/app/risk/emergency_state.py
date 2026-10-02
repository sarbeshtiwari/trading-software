"""Read-only emergency inhibition checks; no activation, reset or execution authority."""

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.core.errors import SafetyError
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.monitoring.gate import get_trading_gate


async def emergency_state(session):
    state = await session.get(SystemState, SINGLETON_ID, with_for_update=True)
    records = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == "paper-emergency",
                )
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(records) or (
        records
        and (
            state is None
            or bool(state.new_entries_blocked) != bool(records[-1].result["entries_blocked"])
            or bool(state.kill_switch_active) != bool(records[-1].result["kill_switch"])
        )
    ):
        get_trading_gate().block("emergency", "EMERGENCY_STATE_INTEGRITY_FAILURE")
        raise SafetyError("EMERGENCY_STATE_INTEGRITY_FAILURE")
    blocked = bool(state and (state.kill_switch_active or state.new_entries_blocked))
    if blocked:
        get_trading_gate().block(
            "emergency",
            "PAPER_KILL_SWITCH_ACTIVE"
            if state.kill_switch_active
            else "PAPER_NEW_ENTRIES_DISABLED",
        )
    return {"entries_blocked": blocked, "kill_switch": bool(state and state.kill_switch_active)}


async def require_no_emergency(session):
    if (await emergency_state(session))["entries_blocked"]:
        raise SafetyError("PAPER_EMERGENCY_ENTRIES_BLOCKED")
