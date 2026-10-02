"""Historical decision inspection; a replayed approval grants no current permission."""

from pydantic import AwareDatetime

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.db.models.decision import RiskDecision
from app.risk.audit import RiskAudit
from app.risk.evidence import decision_integrity
from app.risk.models import Decision


class RiskDecisionIndex(EvidenceModel):
    id: str
    proposal_id: str
    evaluated_at: AwareDatetime
    is_preflight: bool


class RiskDecisionPage(EvidenceModel):
    items: tuple[RiskDecisionIndex, ...]
    has_more: bool


class RiskDecisionInspection(RiskDecisionIndex):
    status: str
    mode: str
    origin: DataOrigin | None = None
    configuration_version: int | None = None
    decision: Decision | None = None
    historical_only: bool = True


def index(row):
    return RiskDecisionIndex(
        id=row.id,
        proposal_id=row.proposal_id,
        evaluated_at=_utc(row.evaluated_at),
        is_preflight=row.is_preflight,
    )


async def inspect_decision(session, identifier, mode):
    row = await session.get(RiskDecision, identifier)
    if row is None or row.mode != mode or _utc(row.evaluated_at) > get_clock().now():
        return None
    result = RiskDecisionInspection(
        **index(row).model_dump(), mode=mode.value, status=await decision_integrity(session, row)
    )
    if result.status != "AUDIT_BOUND":
        return result
    try:
        decision = await RiskAudit().replay(row.id)
        snapshot = row.state_snapshot
        if decision.model_dump(mode="json") != snapshot["decision"]:
            raise ValueError("decision changed during inspection")
        if (
            snapshot["market"]["mode"] != mode.value
            or snapshot["proposal"]["id"] != row.proposal_id
        ):
            raise ValueError("risk evidence identity mismatch")
        origin = DataOrigin(snapshot["market"]["data_origin"])
    except (ValueError, TypeError, KeyError):
        return result.model_copy(update={"status": "REPLAY_MISMATCH"})
    return result.model_copy(
        update={
            "status": "AUDIT_BOUND_REPLAY_VERIFIED",
            "origin": origin,
            "configuration_version": row.risk_config_version,
            "decision": decision,
        }
    )
