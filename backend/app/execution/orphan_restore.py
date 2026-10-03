"""Owner-authorized reconstruction of a missing PAPER projection, never a fill."""

import sqlalchemy as sa

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.data_origin import ExecutionRealism
from app.core.enums import PositionSide, PositionState, TransactionType
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.execution import PaperExecutionSlot, PaperStateRevision
from app.db.models.paper import PAPER_STATE_ID, PaperBrokerState
from app.db.models.trading import Order, Position, Trade
from app.execution.discrepancies import system_row
from app.execution.orphan_review import position_snapshot
from app.execution.position_recovery import build
from app.modes import TradingMode
from app.notifications.outbox import enqueue


class RestorationReceipt(EvidenceModel):
    orphan_position_id: str
    restored_position_id: str
    plan_hash: str
    audit_hash: str
    entries_authorized: bool = False
    protection_verified: bool = False


async def previous_receipt(session, identifier, expected_plan, actor):
    records = list(await session.scalars(sa.select(AuditEvent).where(
        AuditEvent.chain_id == identifier
    ).order_by(AuditEvent.sequence).limit(1001)))
    if len(records) > 1000 or not records or not verify_records(records):
        raise SafetyError("RESTORATION_AUDIT_UNAVAILABLE")
    last = records[-1]
    if last.event_type != "PAPER_ORPHAN_RESTORED":
        return None
    if (last.actor != actor or last.mode != TradingMode.PAPER
            or last.result["plan"]["plan_hash"] != expected_plan):
        raise SafetyError("RESTORATION_RECEIPT_MISMATCH")
    return RestorationReceipt(
        orphan_position_id=identifier,
        restored_position_id=last.result["restored_position"]["id"],
        plan_hash=expected_plan, audit_hash=last.record_hash,
    )


async def verify_broker(session, broker, plan, entry):
    remote_orders = await broker.list_orders()
    if any(not row.status.is_terminal for row in remote_orders):
        raise SafetyError("RESTORATION_REQUIRES_TERMINAL_ORDERS")
    local_orders = list(await session.scalars(sa.select(Order).where(
        Order.mode == TradingMode.PAPER
    ).with_for_update()))
    if any(not row.status.is_terminal for row in local_orders):
        raise SafetyError("RESTORATION_REQUIRES_TERMINAL_ORDERS")
    by_reference = {row.reference_id: row for row in remote_orders}
    if len(by_reference) != len(remote_orders):
        raise SafetyError("RESTORATION_BROKER_ORDER_AMBIGUOUS")
    for order in local_orders:
        remote = by_reference.pop(order.broker_reference_id, None)
        if remote is None:
            if order.submitted_at is not None or order.broker_order_id is not None:
                raise SafetyError("RESTORATION_BROKER_ORDER_MISSING")
            continue
        if any(getattr(order, key) != getattr(remote, key) for key in (
            "broker_order_id", "trading_symbol", "exchange", "segment", "product",
            "transaction_type", "order_type", "quantity", "filled_quantity", "status", "price",
        )):
            raise SafetyError("RESTORATION_BROKER_ORDER_CHANGED")
    if by_reference:
        raise SafetyError("RESTORATION_UNTRACKED_BROKER_ORDER")
    matching = [row for row in broker.account.open_positions() if (
        row.trading_symbol, row.segment, row.product
    ) == (entry.trading_symbol, entry.segment, entry.product)]
    if len(matching) != 1:
        raise SafetyError("RESTORATION_BROKER_POSITION_UNAVAILABLE")
    remote = matching[0]
    if (remote.net_quantity != plan.quantity or remote.average_price != plan.average_price
            or [(lot.quantity, lot.price) for lot in remote.fifo_lots]
            != [(lot.quantity, lot.price) for lot in plan.lots]):
        raise SafetyError("RESTORATION_BROKER_ACCOUNTING_CHANGED")


async def restore(identifier, *, expected_plan, actor, reason, settings, clock, lock):
    if settings.trading_mode != TradingMode.PAPER:
        raise SafetyError("PAPER_RESTORATION_ONLY")
    if len(reason.strip()) < 10:
        raise SafetyError("RESTORATION_REASON_REQUIRED")
    lock.acquire()
    try:
        async with db_session.session_scope() as session:
            await system_row(session)
            previous = await previous_receipt(session, identifier, expected_plan, actor)
            if previous is not None:
                return previous
            revision = await session.get(PaperStateRevision, PAPER_STATE_ID, with_for_update=True)
            snapshot = await session.get(PaperBrokerState, PAPER_STATE_ID, with_for_update=True)
            if revision is None or snapshot is None or snapshot.mode != TradingMode.PAPER:
                raise SafetyError("RESTORATION_BROKER_STATE_UNAVAILABLE")
            orphan = await session.get(Position, identifier, with_for_update=True)
            plan = await build(identifier, clock.utcnow(), actor, session=session)
            if plan.plan_hash != expected_plan:
                raise SafetyError("RESTORATION_PLAN_CHANGED_REVIEW_AGAIN")
            broker = PaperBrokerProvider(settings, clock=clock, state_store=PaperStateStore())
            await broker.connect()
            entry = await session.scalar(sa.select(Order).where(
                Order.position_id == plan.original_position_id, Order.role == "ENTRY"
            ))
            await verify_broker(session, broker, plan, entry)
            proposal = await session.get(Proposal, plan.proposal_id)
            trades = list(await session.scalars(sa.select(Trade).where(
                Trade.position_id == plan.original_position_id
            ).with_for_update()))
            if await session.get(Position, plan.original_position_id) is not None:
                raise SafetyError("RESTORATION_POSITION_ALREADY_EXISTS")
            slot = await session.get(PaperExecutionSlot, "PAPER", with_for_update=True)
            if slot is not None and slot.proposal_id != proposal.id:
                raise SafetyError("RESTORATION_RESERVATION_CONFLICT")
            if slot is None:
                session.add(PaperExecutionSlot(id="PAPER", proposal_id=proposal.id))
            position = Position(
                id=plan.original_position_id, instrument_id=orphan.instrument_id,
                trading_symbol=orphan.trading_symbol,
                segment=orphan.segment, product=orphan.product,
                side=PositionSide.LONG if plan.quantity > 0 else PositionSide.SHORT,
                state=PositionState.OPEN,
                net_quantity=plan.quantity, average_price=plan.average_price,
                bought_quantity=sum(row.quantity for row in trades
                                    if row.transaction_type == TransactionType.BUY),
                sold_quantity=sum(row.quantity for row in trades
                                  if row.transaction_type == TransactionType.SELL),
                realised_pnl=plan.gross_realised_pnl, total_charges=plan.recorded_charges,
                unrealised_pnl=sa.null(), last_price=None, marked_at=None,
                stop_loss_price=plan.historical_protection.stop,
                target_price=plan.historical_protection.target,
                trailing_stop_price=plan.historical_protection.trailing_stop,
                is_protected=False, proposal_id=proposal.id, strategy_id=proposal.strategy_id,
                regime_at_entry=proposal.regime, opened_at=plan.opened_at,
                mode=TradingMode.PAPER, execution_realism=ExecutionRealism.SIMULATED,
                adopted_from_broker=False,
            )
            session.add(position)
            await session.flush()
            await session.refresh(position)
            record = await AuditService(clock).append_in_session(
                session, AuditIdentity(chain_id=identifier, event_type="PAPER_ORPHAN_RESTORED",
                                       actor=actor, mode=TradingMode.PAPER),
                {"position_id": identifier, "proposal_id": proposal.id, "result": {
                    "reason": reason.strip(), "plan": plan.model_dump(mode="json"),
                    "archived_orphan": position_snapshot(orphan),
                    "restored_position": position_snapshot(position),
                    "broker_revision": revision.version,
                    "broker_observed_at": _utc(clock.utcnow()),
                    "trading_authorization": False,
                }},
            )
            await AuditService(clock).append_in_session(
                session, AuditIdentity(
                    chain_id=position.id, event_type="PAPER_POSITION_RESTORED",
                    actor=actor, mode=TradingMode.PAPER,
                ), {"position_id": position.id, "proposal_id": proposal.id, "result": {
                    "orphan_position_id": identifier, "restoration_audit_hash": record.record_hash,
                    "plan_hash": plan.plan_hash, "active_protection_verified": False,
                    "trading_authorization": False,
                }},
            )
            await enqueue(
                session, key=f"orphan-restored:{identifier}", event_type="PAPER_ORPHAN_RESTORED",
                severity="CRITICAL", message=(
                    "PAPER position history restored. Reconciliation review and active protection "
                    "remain required; trading has not been authorized."
                ), clock=clock, source_event=record,
            )
            await session.delete(orphan)
            return RestorationReceipt(
                orphan_position_id=identifier, restored_position_id=position.id,
                plan_hash=plan.plan_hash, audit_hash=record.record_hash,
            )
    finally:
        lock.release()
