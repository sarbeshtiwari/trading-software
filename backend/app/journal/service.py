"""Read recorded lineage and append owner annotations without changing economics."""

from datetime import datetime, time, timedelta

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.clock import IST, UTC, get_clock
from app.db.models.audit import AuditEvent
from app.db.models.decision import ConsideredCandidate, Proposal, RiskDecision, SizingRecord
from app.db.models.journal import JournalAnnotation, JournalEntry
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order, OrderEvent, Position, Trade
from app.journal.corrections import version_history
from app.journal.schemas import AnnotationView, EntryView, JournalDetail
from app.portfolio.journal_integrity import journal_verified


def snapshot(row):
    if row is None:
        return None
    return freeze_snapshot(
        {
            column.name: _utc(value) if isinstance(value, datetime) else value
            for column in row.__table__.columns
            if column.name not in {"approval_token", "request_payload", "response_payload"}
            for value in (getattr(row, column.name),)
        }
    )


async def chain(session, identifier):
    if not identifier:
        return []
    records = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == identifier)
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if not verify_records(records):
        raise ValueError("journal lineage audit integrity failure")
    return records


async def entry_view(session, row):
    history = await version_history(session, row)
    revision = next((item for item in history if item.entry_id == row.id), None)
    events = await chain(session, row.id)
    sealed = bool(events) and events[0].event_type in {
        "PAPER_JOURNAL_RECORDED",
        "REJECTION_JOURNAL_RECORDED",
        "ORDER_ACTION_JOURNAL_RECORDED",
        "JOURNAL_CORRECTION_RECORDED",
    }
    if sealed and not await journal_verified(session, row.id):
        raise ValueError("journal integrity failure")
    notes = list(
        (
            await session.scalars(
                sa.select(JournalAnnotation)
                .where(JournalAnnotation.journal_entry_id == row.id)
                .order_by(JournalAnnotation.created_at, JournalAnnotation.id)
            )
        ).all()
    )
    recorded = {
        event.result["annotation"]["id"]: event.result["annotation"]
        for event in events
        if event.event_type == "JOURNAL_ANNOTATION_ADDED"
    }
    if set(recorded) != {note.id for note in notes}:
        raise ValueError("journal annotation integrity unavailable")
    for note in notes:
        if recorded.get(note.id) != snapshot(note):
            raise ValueError("journal annotation integrity unavailable")
    return EntryView.model_validate(
        snapshot(row)
        | {
            "integrity": "AUDIT_BOUND" if sealed else "LEGACY_UNBOUND",
            "annotations": [AnnotationView.model_validate(snapshot(note)) for note in notes],
            "revision_root_id": revision.root_id if revision else row.id,
            "previous_version_id": revision.previous_id if revision else None,
            "latest_version_id": history[-1].entry_id if history else row.id,
            "record_role": "OWNER_CONTEXT_CORRECTION" if revision else "ORIGINAL",
        }
    )


def selection(*, mode, kind, strategy_id, instrument_id, outcome, start, end):
    query = sa.select(JournalEntry).where(JournalEntry.mode == mode)
    for column, value in (
        (JournalEntry.kind, kind),
        (JournalEntry.strategy_id, strategy_id),
        (JournalEntry.instrument_id, instrument_id),
    ):
        if value is not None:
            query = query.where(column == value)
    if outcome is not None:
        comparison = {
            "WIN": JournalEntry.net_pnl > 0,
            "LOSS": JournalEntry.net_pnl < 0,
            "FLAT": JournalEntry.net_pnl == 0,
            "UNAVAILABLE": JournalEntry.net_pnl.is_(None),
        }
        query = query.where(comparison[outcome])
    observed = sa.func.coalesce(
        JournalEntry.closed_at, JournalEntry.opened_at, JournalEntry.created_at
    )
    if start is not None:
        query = query.where(
            observed >= datetime.combine(start, time.min, tzinfo=IST).astimezone(UTC)
        )
    if end is not None:
        query = query.where(
            observed
            < datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST).astimezone(UTC)
        )
    return query.order_by(observed.desc(), JournalEntry.id)


async def annotate(session, row, body, actor):
    await entry_view(session, row)
    note = JournalAnnotation(
        journal_entry_id=row.id,
        note=freeze_snapshot(body.note),
        tags=freeze_snapshot(list(body.tags)),
        author=actor,
        created_at=get_clock().utcnow(),
    )
    session.add(note)
    await session.flush()
    await session.refresh(note)
    await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=row.id, event_type="JOURNAL_ANNOTATION_ADDED", actor=actor, mode=row.mode
        ),
        {"result": {"annotation": snapshot(note), "reason": body.reason}},
    )
    return AnnotationView.model_validate(snapshot(note))


async def detail(session, row):
    view = await entry_view(session, row)
    context = (row.indicator_snapshot or {}).get("decision_context", {})
    candidate_id = (row.indicator_snapshot or {}).get("candidate_id")
    proposal = await session.get(Proposal, row.proposal_id) if row.proposal_id else None
    candidate = await session.get(ConsideredCandidate, candidate_id) if candidate_id else None
    position = await session.get(Position, row.position_id) if row.position_id else None
    regime = (
        await session.get(RegimeHistory, context["regime_id"]) if context.get("regime_id") else None
    )
    orders = (
        list(
            (
                await session.scalars(
                    sa.select(Order)
                    .where(Order.proposal_id == row.proposal_id, Order.mode == row.mode)
                    .order_by(Order.created_at, Order.id)
                )
            ).all()
        )
        if row.proposal_id
        else []
    )
    identifiers = [order.id for order in orders]
    fills = list(
        (
            await session.scalars(
                sa.select(Trade)
                .where(Trade.order_id.in_(identifiers))
                .order_by(Trade.executed_at, Trade.id)
            )
        ).all()
    )
    transitions = list(
        (
            await session.scalars(
                sa.select(OrderEvent)
                .where(OrderEvent.order_id.in_(identifiers))
                .order_by(OrderEvent.occurred_at, OrderEvent.id)
            )
        ).all()
    )
    risks = (
        list(
            (
                await session.scalars(
                    sa.select(RiskDecision)
                    .where(RiskDecision.proposal_id == row.proposal_id)
                    .order_by(RiskDecision.evaluated_at, RiskDecision.id)
                )
            ).all()
        )
        if row.proposal_id
        else []
    )
    sizing = (
        list(
            (
                await session.scalars(
                    sa.select(SizingRecord).where(SizingRecord.proposal_id == row.proposal_id)
                )
            ).all()
        )
        if row.proposal_id
        else []
    )
    chains = {
        row.id,
        row.audit_chain_id,
        row.proposal_id,
        candidate_id,
        context.get("market", {}).get("source_id"),
        *identifiers,
        row.position_id,
    } - {None}
    audit, missing_audits = [], []
    for identifier in sorted(chains):
        records = await chain(session, identifier)
        audit.extend(snapshot(event) for event in records)
        if not records:
            missing_audits.append(identifier)
    gaps = []
    if view.integrity != "AUDIT_BOUND":
        gaps.append("LEGACY_JOURNAL_UNBOUND")
    if row.proposal_id and proposal is None:
        gaps.append("PROPOSAL_UNAVAILABLE")
    if candidate_id and candidate is None:
        gaps.append("CANDIDATE_UNAVAILABLE")
    if row.kind == "TRADE":
        required_orders = {row.entry_order_id, *(row.exit_order_ids or [])}
        if None in required_orders or not required_orders.issubset(identifiers):
            gaps.append("ORDER_LINK_UNAVAILABLE")
        if position is None or not fills or not risks or not sizing:
            gaps.append("POSITION_FILL_RISK_OR_SIZING_UNAVAILABLE")
        if row.net_pnl is None:
            gaps.append("NET_COST_ACCOUNTING_UNAVAILABLE")
    if missing_audits:
        gaps.append("AUDIT_UNAVAILABLE:" + ",".join(missing_audits))
    return JournalDetail(
        entry=view,
        proposal=snapshot(proposal),
        candidate=snapshot(candidate),
        position=snapshot(position),
        regime=snapshot(regime),
        orders=[snapshot(item) for item in orders],
        fills=[snapshot(item) for item in fills],
        order_events=[snapshot(item) for item in transitions],
        risk_decisions=[snapshot(item) for item in risks],
        sizing=[snapshot(item) for item in sizing],
        audit=audit,
        gaps=gaps,
        lineage_status="INCOMPLETE" if gaps else "AVAILABLE",
        revisions=await version_history(session, row),
    )
