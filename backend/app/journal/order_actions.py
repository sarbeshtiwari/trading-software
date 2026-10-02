"""Order-management observations are not realised trades or invented economics."""

import hashlib

from app.audit.snapshots import freeze_snapshot
from app.db.models.journal import JournalEntry
from app.portfolio.journal_integrity import bind_journal


async def record_order_action(session, order, event, clock):
    identifier = "jrn" + hashlib.sha256(event.chain_id.encode()).hexdigest()[:32]
    session.add(
        JournalEntry(
            id=identifier,
            kind="ORDER_ACTION",
            mode=order.mode,
            proposal_id=order.proposal_id,
            risk_decision_id=order.risk_decision_id,
            entry_order_id=order.id,
            audit_chain_id=event.chain_id,
            instrument_id=order.instrument_id,
            trading_symbol=order.trading_symbol,
            strategy_id=order.strategy_id,
            outcome=event.result["status"],
            indicator_snapshot=freeze_snapshot(
                {"order_action": event.result, "audit_id": event.id}
            ),
            created_at=clock.utcnow(),
        )
    )
    await bind_journal(
        session,
        identifier,
        clock,
        mode=order.mode,
        event_type="ORDER_ACTION_JOURNAL_RECORDED",
        actor="paper_order_hygiene",
    )
