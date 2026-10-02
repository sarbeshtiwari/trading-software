"""Journal the shared pipeline's actual negative outcomes in its transaction."""

import hashlib

import sqlalchemy as sa

from app.audit.snapshots import freeze_snapshot
from app.db.models.decision import RiskDecision
from app.db.models.journal import JournalEntry
from app.portfolio.journal_integrity import bind_journal


async def record_rejection(
    session, candidate, context, clock, *, proposal_id=None, approved_quantity=0
):
    if approved_quantity:
        return None
    identifier = "jrn" + hashlib.sha256(candidate.id.encode()).hexdigest()[:32]
    if await session.get(JournalEntry, identifier):
        return identifier
    await session.flush()
    risk = (
        await session.scalar(
            sa.select(RiskDecision).where(
                RiskDecision.proposal_id == proposal_id, RiskDecision.is_preflight.is_(False)
            )
        )
        if proposal_id
        else None
    )
    session.add(
        JournalEntry(
            id=identifier,
            kind="REJECTION",
            proposal_id=proposal_id,
            risk_decision_id=risk.id if risk else None,
            audit_chain_id=proposal_id or candidate.id,
            instrument_id=candidate.instrument_id,
            trading_symbol=candidate.trading_symbol,
            strategy_id=context.strategy.id,
            strategy_version=context.strategy.version,
            mode=context.market.mode,
            rejection_code=candidate.reason_code,
            rejection_rule=risk.binding_rule if risk else candidate.stopped_at_stage,
            rejection_detail=f"{candidate.stopped_at_stage}: {candidate.reason_code}",
            indicator_snapshot=freeze_snapshot(
                {
                    "candidate_id": candidate.id,
                    "decision_context": context.model_dump(mode="json"),
                }
            ),
            created_at=clock.utcnow(),
        )
    )
    await bind_journal(
        session,
        identifier,
        clock,
        mode=context.market.mode,
        event_type="REJECTION_JOURNAL_RECORDED",
        actor="decision_pipeline",
    )
    return identifier
