"""Reconstruct economic evidence; a plan does not repair state or authorize orders."""

import hashlib
from decimal import Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.snapshots import canonical, freeze_snapshot
from app.core.errors import SafetyError
from app.core.money import quantize_money
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order, Position, Trade
from app.execution.orphan_review import inspect
from app.execution.paper import replay_fills, stable_id
from app.execution.recovery_protection import HistoricalProtection, reconstruct
from app.modes import TradingMode
from app.portfolio.fifo import Result
from app.portfolio.fill_evidence import fill_snapshot


class RecoveryLot(EvidenceModel):
    source_fill_id: str
    quantity: int
    price: Decimal


class PositionRecoveryPlan(EvidenceModel):
    orphan_position_id: str
    original_position_id: str
    proposal_id: str
    quantity: int
    average_price: Decimal
    gross_realised_pnl: Decimal
    recorded_charges: Decimal
    charges_complete: bool
    opened_at: AwareDatetime
    historical_protection: HistoricalProtection
    lots: tuple[RecoveryLot, ...]
    source_fill_ids: tuple[str, ...]
    source_audit_hashes: tuple[str, ...]
    orphan_review_hash: str
    plan_hash: str
    blockers: tuple[str, ...] = (
        "BROKER_STATE_REVALIDATION_REQUIRED", "PROTECTION_RECONSTRUCTION_REQUIRED",
        "EXPLICIT_RESTORATION_REQUIRED", "ENTRIES_BLOCKED",
    )
    scope: str = "Verified historical accounting plan only; no repair or trading authorization."


async def records_for(session, identifier, now):
    records = list(await session.scalars(sa.select(AuditEvent).where(
        AuditEvent.chain_id == identifier
    ).order_by(AuditEvent.sequence).limit(1001)))
    if (not records or len(records) > 1000 or not verify_records(records)
            or any(record.mode != TradingMode.PAPER or record.actor != "paper_execution"
                   or _utc(record.occurred_at) > now for record in records)):
        raise SafetyError("RECOVERY_AUDIT_EVIDENCE_INVALID")
    return records


def require_fill_totals(orders, trades):
    for order in orders.values():
        filled = sum(trade.quantity for trade in trades if trade.order_id == order.id)
        if filled != order.filled_quantity:
            raise SafetyError("RECOVERY_FILL_HISTORY_INCOMPLETE")


async def require_source_orders(session, entry, verified_orders, trades):
    rows = list(await session.scalars(sa.select(Order).where(
        Order.position_id == entry.position_id
    ).limit(1001)))
    if len(rows) > 1000 or entry.id not in verified_orders:
        raise SafetyError("RECOVERY_FILL_HISTORY_INCOMPLETE")
    require_fill_totals({row.id: row for row in rows}, trades)


async def build(identifier, now, owner):
    async with db_session.session_scope() as session:
        position = await session.get(Position, identifier)
        review = await inspect(session, position, now, owner)
        if not review.acknowledged:
            raise SafetyError("ORPHAN_ACKNOWLEDGMENT_REQUIRED")
        entries = list(await session.scalars(sa.select(Order).where(
            Order.mode == TradingMode.PAPER, Order.role == "ENTRY",
            Order.instrument_id == position.instrument_id, Order.product == position.product,
            Order.segment == position.segment, Order.position_id.is_not(None),
            ~sa.exists().where(Position.id == Order.position_id),
        ).limit(2)))
        if len(entries) != 1:
            raise SafetyError("RECOVERY_HISTORY_MISSING_OR_AMBIGUOUS")
        entry = entries[0]
        if not entry.proposal_id or entry.position_id != stable_id("pos", entry.proposal_id):
            raise SafetyError("RECOVERY_POSITION_LINEAGE_INVALID")
        trades = list(await session.scalars(sa.select(Trade).where(
            Trade.position_id == entry.position_id
        ).limit(1001)))
        if not trades or len(trades) > 1000:
            raise SafetyError("RECOVERY_FILL_HISTORY_UNAVAILABLE")
        hashes, orders = [], {}
        for trade in trades:
            if (trade.mode != TradingMode.PAPER or trade.instrument_id != position.instrument_id
                    or _utc(trade.executed_at) > now or trade.quantity <= 0 or trade.price <= 0):
                raise SafetyError("RECOVERY_FILL_INVALID")
            order = await session.get(Order, trade.order_id)
            if (order is None or order.mode != TradingMode.PAPER
                    or order.position_id != entry.position_id
                    or order.proposal_id != entry.proposal_id
                    or order.instrument_id != position.instrument_id
                    or order.trading_symbol != position.trading_symbol
                    or order.segment != position.segment or order.product != position.product
                    or order.transaction_type != trade.transaction_type):
                raise SafetyError("RECOVERY_ORDER_LINEAGE_INVALID")
            if order.id not in orders:
                records = await records_for(session, order.id, now)
                syncs = [record for record in records if record.event_type == "ORDER_SYNCHRONIZED"]
                if (not syncs or any(record.order_id != order.id
                        or record.proposal_id != order.proposal_id for record in records)
                        or syncs[-1].result.get("status") != order.status.value
                        or syncs[-1].result.get("filled_quantity") != order.filled_quantity):
                    raise SafetyError("RECOVERY_ORDER_EVIDENCE_INVALID")
                orders[order.id] = order
                hashes.append(records[-1].record_hash)
            records = await records_for(session, trade.id, now)
            expected_fill = freeze_snapshot(fill_snapshot(trade, order.trading_symbol))
            if (len(records) != 1 or records[0].event_type != "PAPER_FILL_RECORDED"
                    or records[0].position_id != entry.position_id
                    or records[0].order_id != order.id
                    or records[0].result != expected_fill):
                raise SafetyError("RECOVERY_FILL_EVIDENCE_INVALID")
            hashes.append(records[0].record_hash)
        require_fill_totals(orders, trades)
        await require_source_orders(session, entry, orders, trades)
        lots, realised = replay_fills(trades)
        result = Result(lots, ())
        quantum = Decimal(1).scaleb(-Position.__table__.c.average_price.type.scale)
        if (result.quantity != position.net_quantity
                or abs(result.average_price - position.average_price) > quantum / 2):
            raise SafetyError("RECOVERY_ACCOUNTING_DISAGREES_WITH_ORPHAN")
        ordered = sorted(trades, key=lambda trade: trade.cost_breakdown["fifo"]["sequence"])
        unsigned = PositionRecoveryPlan(
            plan_hash="",
            orphan_position_id=identifier, original_position_id=entry.position_id,
            proposal_id=entry.proposal_id, quantity=result.quantity,
            average_price=result.average_price,
            gross_realised_pnl=quantize_money(realised),
            recorded_charges=sum((trade.total_charges for trade in trades), Decimal(0)),
            charges_complete=all(
                trade.cost_breakdown.get("status") == "ESTIMATED" for trade in trades
            ),
            opened_at=_utc(ordered[0].executed_at),
            historical_protection=await reconstruct(
                session, entry, _utc(ordered[0].executed_at), now
            ),
            lots=tuple(RecoveryLot(
                source_fill_id=lot.source_id, quantity=lot.quantity, price=lot.price
            ) for lot in lots),
            source_fill_ids=tuple(trade.id for trade in ordered),
            source_audit_hashes=tuple(sorted(hashes)), orphan_review_hash=review.head_hash,
        )
        digest = hashlib.sha256(canonical(unsigned.model_dump(mode="json")).encode()).hexdigest()
        return unsigned.model_copy(update={
            "plan_hash": digest
        })
