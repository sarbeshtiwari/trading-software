"""Historical protection evidence; never a certificate of active supervision."""

from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.core.errors import SafetyError
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal, RiskDecision
from app.execution.paper import approved_risk
from app.modes import TradingMode
from app.risk.evidence import decision_integrity
from app.strategies.exits import ExitDecision


class HistoricalProtection(EvidenceModel):
    stop: Decimal
    target: Decimal
    trailing_stop: Decimal | None
    risk_decision_id: str
    position_audit_head: str | None
    active_protection_verified: bool = False


async def reconstruct(session, entry, opened_at, now):
    proposal = await session.get(Proposal, entry.proposal_id)
    risk = await session.get(RiskDecision, entry.risk_decision_id)
    if (proposal is None or risk is None or proposal.mode != TradingMode.PAPER
            or risk.mode != TradingMode.PAPER or risk.proposal_id != proposal.id
            or _utc(risk.evaluated_at) > now
            or await decision_integrity(session, risk) != "AUDIT_BOUND"):
        raise SafetyError("RECOVERY_PROTECTION_APPROVAL_UNAVAILABLE")
    await approved_risk(proposal, risk)
    records = list(await session.scalars(sa.select(AuditEvent).where(
        AuditEvent.chain_id == entry.position_id
    ).order_by(AuditEvent.sequence).limit(1001)))
    if (len(records) > 1000 or (records and not verify_records(records))
            or any(record.mode != TradingMode.PAPER
                   or record.position_id != entry.position_id
                   or record.proposal_id != proposal.id
                   or _utc(record.occurred_at) > now for record in records)):
        raise SafetyError("RECOVERY_PROTECTION_HISTORY_INVALID")
    trailing = None
    updated_at = opened_at
    for record in records:
        if record.event_type != "REFERENCE_EXIT_STATE":
            continue
        decision = ExitDecision.model_validate(record.result)
        state = decision.state
        if (record.actor != "reference_exit_monitor"
                or state.position_id != entry.position_id
                or state.direction != proposal.direction
                or state.stop != proposal.stop_loss or state.target != proposal.target_price
                or state.opened_at != opened_at
                or not updated_at <= state.updated_at <= _utc(record.occurred_at)):
            raise SafetyError("RECOVERY_TRAILING_HISTORY_INVALID")
        sign = 1 if state.direction.value == "LONG" else -1
        previous = trailing if trailing is not None else proposal.stop_loss
        if not decision.trailing_stop.is_finite() or sign * (decision.trailing_stop - previous) < 0:
            raise SafetyError("RECOVERY_TRAILING_STOP_REGRESSION")
        trailing, updated_at = decision.trailing_stop, state.updated_at
    return HistoricalProtection(
        stop=proposal.stop_loss, target=proposal.target_price, trailing_stop=trailing,
        risk_decision_id=risk.id,
        position_audit_head=records[-1].record_hash if records else None,
    )
