"""Read durable original execution history; never accept counts from an agent."""

from datetime import timedelta

import sqlalchemy as sa

from app.agents.validation import _utc
from app.core.clock import IST, UTC, get_clock
from app.core.data_origin import DataOrigin
from app.core.entry_windows import entry_window
from app.core.enums import OrderStatus
from app.db.models.decision import Proposal
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.portfolio.journal_integrity import journal_verified
from app.risk.entry_models import ClosedOutcome, EntryEvidence, EntryPolicy


async def load_entry_history(session, market, settings, *, exclude_order_id=None, calendar=None):
    policy = EntryPolicy(
        maximum_daily_entries=settings.max_trades_per_day,
        loss_cooloff_minutes=settings.loss_cooloff_minutes,
    )
    as_of = market.as_of.astimezone(UTC)
    identity = {
        "window": entry_window(as_of, settings, calendar=calendar),
        "policy": policy,
        "as_of": as_of,
        "mode": market.mode,
        "data_origin": market.data_origin,
        "instrument_id": market.instrument_id,
    }
    if as_of > get_clock().utcnow():
        return EntryEvidence(
            **identity,
            counted_order_ids=(),
            closed_outcomes=(),
            unavailable_reason="HISTORY_TIME_IN_FUTURE",
        )
    start = as_of.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    filled_today = sa.exists(
        sa.select(Trade.id).where(
            Trade.order_id == Order.id,
            Trade.executed_at >= start,
            Trade.executed_at <= as_of,
            Trade.created_at <= as_of,
        )
    )
    orders = list(
        (
            await session.scalars(
                sa.select(Order)
                .where(
                    Order.mode == market.mode,
                    Order.role == "ENTRY",
                    Order.created_at <= as_of,
                    sa.or_(
                        Order.created_at >= start,
                        ~Order.status.in_(
                            [OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
                        ),
                        Order.updated_at > as_of,
                        filled_today,
                    ),
                )
                .order_by(Order.id)
                .limit(10001)
            )
        ).all()
    )
    filled = set(
        (
            await session.scalars(
                sa.select(Trade.order_id)
                .where(
                    Trade.order_id.in_([order.id for order in orders]),
                    Trade.executed_at <= as_of,
                    Trade.created_at <= as_of,
                )
                .distinct()
            )
        ).all()
    )
    counted, unavailable = count_orders(orders, filled, market.data_origin, as_of, exclude_order_id)
    outcomes, outcome_error = await closed_outcomes(session, market, policy, as_of)
    return EntryEvidence(
        **identity,
        counted_order_ids=tuple(counted),
        closed_outcomes=tuple(outcomes),
        unavailable_reason=unavailable or outcome_error,
    )


async def closed_outcomes(session, market, policy, as_of):
    if not policy.loss_cooloff_minutes:
        return [], None
    unavailable = None
    outcomes = []
    journals = list(
        (
            await session.scalars(
                sa.select(JournalEntry)
                .where(
                    JournalEntry.mode == market.mode,
                    JournalEntry.kind == "TRADE",
                    JournalEntry.version == 1,
                    JournalEntry.instrument_id == market.instrument_id,
                    JournalEntry.created_at <= as_of,
                    JournalEntry.closed_at <= as_of,
                    JournalEntry.closed_at
                    >= as_of - timedelta(minutes=policy.loss_cooloff_minutes),
                )
                .order_by(JournalEntry.id)
                .limit(10001)
            )
        ).all()
    )
    if len(journals) > 10000:
        unavailable = "OUTCOME_HISTORY_BOUND_EXCEEDED"
    for journal in journals:
        origin = (
            (journal.indicator_snapshot or {})
            .get("decision_context", {})
            .get("market", {})
            .get("data_origin")
        )
        if origin not in {item.value for item in DataOrigin}:
            unavailable = "OUTCOME_ORIGIN_UNAVAILABLE"
        if origin != market.data_origin.value:
            continue
        if not await journal_verified(session, journal.id):
            unavailable = "OUTCOME_AUDIT_UNAVAILABLE"
        outcomes.append(
            ClosedOutcome(
                journal_id=journal.id, closed_at=_utc(journal.closed_at), net_pnl=journal.net_pnl
            )
        )
    closed = list(
        (
            await session.execute(
                sa.select(Position.id, Proposal.context_snapshot)
                .outerjoin(Proposal, Proposal.id == Position.proposal_id)
                .where(
                    Position.mode == market.mode,
                    Position.instrument_id == market.instrument_id,
                    Position.created_at <= as_of,
                    Position.closed_at <= as_of,
                    Position.closed_at >= as_of - timedelta(minutes=policy.loss_cooloff_minutes),
                )
                .limit(10001)
            )
        ).all()
    )
    matched = {journal.position_id for journal in journals}
    if len(closed) > 10000:
        unavailable = "CLOSED_POSITION_HISTORY_BOUND_EXCEEDED"
    for identifier, context in closed:
        origin = (context or {}).get("market", {}).get("data_origin")
        if origin not in {item.value for item in DataOrigin} or (
            origin == market.data_origin.value and identifier not in matched
        ):
            unavailable = "CLOSED_POSITION_JOURNAL_UNAVAILABLE"
    return outcomes, unavailable


def count_orders(orders, filled, data_origin, as_of, exclude_order_id):
    counted = []
    unavailable = "ENTRY_HISTORY_BOUND_EXCEEDED" if len(orders) > 10000 else None
    for order in orders:
        if order.id == exclude_order_id:
            continue
        origin = (order.request_payload or {}).get("data_origin")
        if origin not in {item.value for item in DataOrigin}:
            unavailable = "ORDER_ORIGIN_UNAVAILABLE"
        if origin != data_origin.value:
            continue
        if _utc(order.updated_at or order.created_at) > as_of:
            unavailable = "ORDER_STATE_NEWER_THAN_DECISION"
        if order.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED} and (
            _utc(order.updated_at or order.created_at) <= as_of and order.id not in filled
        ):
            continue
        if order.status == OrderStatus.EXECUTED and order.id not in filled:
            unavailable = "EXECUTED_ORDER_FILLS_UNAVAILABLE"
        counted.append(order.id)
    return counted, unavailable
