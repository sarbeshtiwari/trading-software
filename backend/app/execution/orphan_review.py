"""Owner acknowledgment of immutable orphan evidence, never trading authorization."""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.enums import PositionSide, PositionState
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.trading import Position
from app.execution.discrepancies import system_row
from app.modes import TradingMode


class OrphanReview(EvidenceModel):
    position_id: str
    trading_symbol: str
    quantity: int
    observed_average_price: Decimal
    observed_at: AwareDatetime
    head_hash: str
    acknowledged: bool
    acknowledged_by: str | None
    acknowledged_at: AwareDatetime | None
    blockers: tuple[str, ...] = (
        "HISTORY_NOT_RECONSTRUCTED", "PROTECTION_UNVERIFIED", "ENTRIES_BLOCKED",
    )
    scope: str = "Historical PAPER observation. Acknowledgment does not repair or arm trading."


class OrphanReviewPage(EvidenceModel):
    items: tuple[OrphanReview, ...]
    has_more: bool


def position_snapshot(position):
    return freeze_snapshot({
        column.name: _utc(value) if isinstance(value, datetime) else value
        for column in Position.__table__.columns
        for value in [getattr(position, column.name)]
    })


async def inspect(session, position, now, owner):
    if (position is None or position.mode != TradingMode.PAPER
            or not position.adopted_from_broker or position.state != PositionState.ADOPTED):
        raise SafetyError("ADOPTED_PAPER_POSITION_REQUIRED")
    records = list(await session.scalars(sa.select(AuditEvent).where(
        AuditEvent.chain_id == position.id
    ).order_by(AuditEvent.sequence).limit(1001)))
    if not records or len(records) > 1000 or not verify_records(records):
        raise SafetyError("ORPHAN_EVIDENCE_INVALID")
    for record in records:
        expected = {
            "PAPER_ORPHAN_ADOPTED": "paper_reconciliation",
            "PAPER_ORPHAN_ACKNOWLEDGED": owner,
        }.get(record.event_type)
        if (expected is None or record.actor != expected or record.mode != TradingMode.PAPER
                or record.position_id != position.id or _utc(record.occurred_at) > now):
            raise SafetyError("ORPHAN_EVIDENCE_INVALID")
    first = records[0]
    if first.event_type != "PAPER_ORPHAN_ADOPTED":
        raise SafetyError("ORPHAN_SOURCE_REQUIRED")
    source = first.result
    instrument = await session.get(Instrument, position.instrument_id)
    if (instrument is None or instrument.exchange.value != source["exchange"]
            or instrument.trading_symbol != position.trading_symbol
            or instrument.segment != position.segment
            or position.trading_symbol != source["symbol"]
            or position.segment.value != source["segment"]
            or position.product.value != source["product"]
            or position.net_quantity != source["quantity"]
            or position.side != (
                PositionSide.LONG if position.net_quantity > 0 else PositionSide.SHORT
            )
            or abs(position.average_price - Decimal(source["average_price"])) > (
                Decimal(1).scaleb(-Position.__table__.c.average_price.type.scale) / 2
            )):
        raise SafetyError("ORPHAN_POSITION_CHANGED_REVIEW_REQUIRED")
    if position.is_protected or any(value is not None for value in (
        position.realised_pnl, position.unrealised_pnl, position.total_charges,
        position.proposal_id, position.strategy_id, position.opened_at, position.closed_at,
        position.stop_loss_price, position.target_price,
        position.trailing_stop_price, position.protective_order_id,
    )):
        raise SafetyError("ORPHAN_HISTORY_OR_PROTECTION_CHANGED")
    last = records[-1]
    acknowledged = last.event_type == "PAPER_ORPHAN_ACKNOWLEDGED"
    if acknowledged and last.result.get("position") != position_snapshot(position):
        raise SafetyError("ORPHAN_POSITION_CHANGED_REVIEW_REQUIRED")
    return OrphanReview(
        position_id=position.id, trading_symbol=position.trading_symbol,
        quantity=position.net_quantity, observed_average_price=position.average_price,
        observed_at=_utc(first.occurred_at), head_hash=last.record_hash,
        acknowledged=acknowledged, acknowledged_by=last.actor if acknowledged else None,
        acknowledged_at=_utc(last.occurred_at) if acknowledged else None,
    )


async def listing(offset, now, owner):
    async with db_session.session_scope() as session:
        rows = list(await session.scalars(sa.select(Position).where(
            Position.mode == TradingMode.PAPER, Position.adopted_from_broker.is_(True)
        ).order_by(Position.id).offset(offset).limit(51)))
        items = [await inspect(session, row, now, owner) for row in rows[:50]]
        return OrphanReviewPage(items=tuple(items), has_more=len(rows) > 50)


async def acknowledge(identifier, *, actor, reason, expected_head, clock):
    async with db_session.session_scope() as session:
        await system_row(session)
        position = await session.get(Position, identifier, with_for_update=True)
        review = await inspect(session, position, clock.utcnow(), actor)
        if review.acknowledged:
            return review
        if review.head_hash != expected_head:
            raise SafetyError("ORPHAN_EVIDENCE_CHANGED_REVIEW_AGAIN")
        await AuditService(clock).append_in_session(
            session, AuditIdentity(
                chain_id=identifier, event_type="PAPER_ORPHAN_ACKNOWLEDGED",
                actor=actor, mode=TradingMode.PAPER,
            ), {"position_id": identifier, "result": {
                "position": position_snapshot(position), "reason": reason,
                "trading_authorization": False,
            }},
        )
        return await inspect(session, position, clock.utcnow(), actor)
