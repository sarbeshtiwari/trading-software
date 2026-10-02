"""Bounded read-only traversal; chain consistency is not external verification."""

from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel, JsonValue

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.snapshots import freeze_snapshot
from app.core.clock import get_clock
from app.db.models.audit import AuditEvent
from app.db.models.decision import ConsideredCandidate, Proposal, RiskDecision
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.portfolio.journal_integrity import journal_verified


class AuditScopeTooLarge(ValueError):
    pass


class AuditItem(BaseModel):
    id: str
    chain_id: str
    sequence: int
    occurred_at: AwareDatetime
    event_type: str
    actor: str
    mode: str
    decision: str | None
    proposal_id: str | None
    instrument_id: str | None
    order_id: str | None
    position_id: str | None


class AuditRecord(AuditItem):
    snapshot: dict[str, JsonValue]


class AuditPage(BaseModel):
    events: list[AuditItem]
    has_more: bool


class AuditChainView(BaseModel):
    id: str
    integrity: Literal["CONSISTENT", "MISSING", "CORRUPT"]
    records: list[AuditRecord]


class JournalEvidence(BaseModel):
    id: str
    integrity: Literal["CONSISTENT", "CORRUPT"]
    snapshot: dict[str, JsonValue] | None


class AuditTrail(BaseModel):
    requested_id: str
    chains: list[AuditChainView]
    journals: list[JournalEvidence]
    gaps: list[str]
    scope: str = "Stored chain consistency only; not proof of completeness or external truth"


def snapshot(row):
    return freeze_snapshot(
        {
            column.name: _utc(value) if isinstance(value, datetime) else value
            for column in row.__table__.columns
            for value in (getattr(row, column.name),)
        }
    )


def item(row):
    return AuditItem.model_validate(snapshot(row))


async def resolve(session, identifier, mode):
    event = await session.get(AuditEvent, identifier)
    if event and event.mode == mode:
        return event.proposal_id, event.chain_id
    for model in (Proposal, ConsideredCandidate, RiskDecision, Order, Position, Trade):
        row = await session.get(model, identifier)
        if row is None or getattr(row, "mode", mode) != mode:
            continue
        if model is Proposal:
            return row.id, row.id
        if model is Trade:
            order = await session.get(Order, row.order_id)
            return order.proposal_id if order and order.mode == mode else None, row.id
        return getattr(row, "proposal_id", None), identifier if model in (
            ConsideredCandidate,
            Order,
        ) else None
    event = await session.scalar(
        sa.select(AuditEvent)
        .where(AuditEvent.chain_id == identifier, AuditEvent.mode == mode)
        .limit(1)
    )
    return (event.proposal_id, identifier) if event else (None, None)


async def trail(session, identifier, mode):
    proposal_id, root = await resolve(session, identifier, mode)
    if root is None and proposal_id is None:
        return None
    chain_ids = {value for value in (root, proposal_id) if value}
    journals = []
    if proposal_id:
        orders = list(
            (
                await session.scalars(
                    sa.select(Order).where(Order.proposal_id == proposal_id, Order.mode == mode)
                )
            ).all()
        )
        positions = list(
            await session.scalars(
                sa.select(Position.id).where(
                    Position.proposal_id == proposal_id, Position.mode == mode
                )
            )
        )
        order_ids = [row.id for row in orders]
        chain_ids.update(order_ids)
        chain_ids.update(
            await session.scalars(
                sa.select(Trade.id).where(Trade.order_id.in_(order_ids), Trade.mode == mode)
            )
        )
        chain_ids.update(
            await session.scalars(
                sa.select(AuditEvent.chain_id).where(
                    AuditEvent.mode == mode,
                    sa.or_(
                        AuditEvent.proposal_id == proposal_id,
                        AuditEvent.order_id.in_(order_ids),
                        AuditEvent.position_id.in_(positions),
                    ),
                )
            )
        )
        journals = list(
            (
                await session.scalars(
                    sa.select(JournalEntry).where(
                        JournalEntry.proposal_id == proposal_id, JournalEntry.mode == mode
                    )
                )
            ).all()
        )
        chain_ids.update(row.id for row in journals)
    if len(chain_ids) > 100:
        raise AuditScopeTooLarge("AUDIT_TRAIL_TOO_LARGE")
    chains, gaps, sources = [], [], set()
    for chain_id in sorted(chain_ids):
        chain = await read_chain(session, chain_id, mode)
        chains.append(chain)
        if chain.integrity != "CONSISTENT":
            gaps.append(f"{chain_id}: {chain.integrity}")
        for record in chain.records:
            used = record.snapshot.get("data_used") or {}
            context = used.get("context") if isinstance(used, dict) else None
            market = context.get("market") if isinstance(context, dict) else None
            source = market.get("source_id") if isinstance(market, dict) else None
            if isinstance(source, str) and source not in chain_ids:
                sources.add(source)
    if len(chain_ids | sources) > 100 or sum(len(chain.records) for chain in chains) > 2000:
        raise AuditScopeTooLarge("AUDIT_TRAIL_TOO_LARGE")
    for source in sorted(sources):
        chain = await read_chain(session, source, mode)
        chains.append(chain)
        if sum(len(value.records) for value in chains) > 2000:
            raise AuditScopeTooLarge("AUDIT_TRAIL_TOO_LARGE")
        if chain.integrity != "CONSISTENT":
            gaps.append(f"Market source {source}: {chain.integrity}; inspect embedded snapshot")
    linked = await read_journals(session, journals, gaps)
    return AuditTrail(requested_id=identifier, chains=chains, journals=linked, gaps=gaps)


async def read_journals(session, journals, gaps):
    linked = []
    for journal in journals:
        try:
            verified = await journal_verified(session, journal.id)
        except (ValueError, TypeError, KeyError):
            verified = False
        linked.append(
            JournalEvidence(
                id=journal.id,
                integrity="CONSISTENT" if verified else "CORRUPT",
                snapshot=snapshot(journal) if verified else None,
            )
        )
        if not verified:
            gaps.append(f"Journal {journal.id}: CORRUPT")
    return linked


async def read_chain(session, identifier, mode):
    rows = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == identifier, AuditEvent.mode == mode)
                .order_by(AuditEvent.sequence)
                .limit(2001)
            )
        ).all()
    )
    if len(rows) > 2000:
        raise AuditScopeTooLarge("AUDIT_CHAIN_TOO_LARGE")
    try:
        verified = verify_records(rows) and all(
            _utc(row.occurred_at) <= get_clock().utcnow() for row in rows
        )
    except (ValueError, TypeError, KeyError):
        verified = False
    status = "MISSING" if not rows else "CONSISTENT" if verified else "CORRUPT"
    return AuditChainView(
        id=identifier,
        integrity=status,
        records=[AuditRecord(**item(row).model_dump(), snapshot=snapshot(row)) for row in rows]
        if status == "CONSISTENT"
        else [],
    )
