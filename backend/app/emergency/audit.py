"""Independent request outcomes never overwrite the authoritative control chain."""

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.ids import new_id


async def record_refusal(*, actor, action, reason, outcome, code):
    return await AuditService().append(
        AuditIdentity(
            chain_id=new_id("emg"),
            event_type="EMERGENCY_REQUEST_REFUSED",
            actor=actor,
            mode=get_settings().trading_mode,
            severity="WARNING",
        ),
        {
            "decision": "FAILED" if outcome == "FAILED_REVIEW_REQUIRED" else "REJECTED",
            "result": {
                "action": action,
                "reason": reason,
                "outcome": outcome,
                "code": code,
                "state_change": "NOT_ASSERTED_INSPECT_CONTROL_CHAIN",
                "control_chain_id": "paper-emergency",
            },
        },
    )
