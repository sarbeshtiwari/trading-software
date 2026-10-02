"""Owner-declared PAPER evidence policy, versioned on an immutable audit chain."""

import hashlib
from decimal import Decimal

import sqlalchemy as sa
from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.modes import TradingMode
from app.strategies.base import StrategySpec
from app.strategies.registry import specification_hash


def evidence_chain(prefix, identity):
    return prefix + hashlib.sha256(identity.encode()).hexdigest()[:32]


class PaperEvidencePolicy(EvidenceModel):
    version: int = Field(gt=0, strict=True)
    minimum_sessions: int = Field(gt=0, strict=True)
    minimum_trades: int = Field(gt=0, strict=True)
    minimum_session_coverage_fraction: Decimal = Field(gt=0, le=1)
    sample_interval_seconds: int = Field(ge=30, le=300, strict=True)
    maximum_sample_gap_seconds: int = Field(ge=30, le=600, strict=True)

    @model_validator(mode="after")
    def coherent(self):
        if self.maximum_sample_gap_seconds < self.sample_interval_seconds:
            raise ValueError("maximum gap cannot be shorter than sample interval")
        return self


async def registration(session, strategy_id, version):
    row = await session.scalar(
        sa.select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == strategy_id, StrategyRegistration.version == version
        )
    )
    if (
        row is None
        or specification_hash(StrategySpec.model_validate(row.parameters)) != row.parameter_hash
    ):
        raise ValueError("registered immutable strategy unavailable")
    return row


async def policy_records(session, row):
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == evidence_chain("pep", row.id))
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(events):
        raise ValueError("PAPER evidence policy integrity failure")
    return events


async def current_policy(session, row):
    events = await policy_records(session, row)
    if not events:
        return None, None
    latest = events[-1]
    if latest.result["parameter_hash"] != row.parameter_hash:
        raise ValueError("PAPER policy parameter mismatch")
    return PaperEvidencePolicy.model_validate(latest.result["policy"]), latest


async def publish_policy(session, row, policy, *, actor, reason):
    events = await policy_records(session, row)
    if policy.version != len(events) + 1:
        raise ValueError("next sequential PAPER evidence policy version required")
    row.live_approved = row.enabled_live = False
    row.approved_at = row.approved_by = row.approval_reason = None
    return await AuditService(get_clock()).append_in_session(
        session,
        AuditIdentity(
            chain_id=evidence_chain("pep", row.id),
            event_type="PAPER_EVIDENCE_POLICY",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "strategy_id": row.strategy_id,
            "result": {
                "registration_id": row.id,
                "parameter_hash": row.parameter_hash,
                "policy": policy.model_dump(mode="json"),
                "reason": reason,
                "live_approved": False,
            },
        },
        expected_count=len(events),
    )
