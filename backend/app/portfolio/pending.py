"""Unfilled PAPER entry commitments derived from sealed preflight evidence."""

from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.core.enums import OrderStatus, OrderType, SignalDirection, TransactionType
from app.core.errors import SafetyError
from app.db.models.decision import RiskDecision
from app.db.models.trading import Order
from app.modes import TradingMode
from app.risk.evidence import decision_integrity
from app.risk.models import Exposure, RiskProposal


@dataclass(frozen=True)
class PendingEntry:
    proposal_id: str
    strategy_id: str
    exposure: Exposure
    margin: Decimal
    risk: Decimal


async def pending_entries(session, *, now, origin):
    orders = list(await session.scalars(sa.select(Order).where(
        Order.mode == TradingMode.PAPER,
        Order.role == "ENTRY",
        ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
    )))
    entries = []
    for order in orders:
        remaining = order.quantity - order.filled_quantity
        if remaining <= 0:
            continue
        decision = await session.get(RiskDecision, order.risk_decision_id)
        if (
            decision is None or not decision.approved or not decision.is_preflight
            or decision.mode != TradingMode.PAPER or decision.proposal_id != order.proposal_id
            or _utc(decision.evaluated_at) > now
            or any(_utc(moment) > now for moment in (order.created_at, order.submitted_at)
                   if moment is not None)
            or await decision_integrity(session, decision) != "AUDIT_BOUND"
        ):
            raise SafetyError("PENDING_ENTRY_EVIDENCE_UNAVAILABLE")
        try:
            proposal = RiskProposal.model_validate(decision.state_snapshot["proposal"])
        except (KeyError, TypeError, ValueError) as error:
            raise SafetyError("PENDING_ENTRY_EVIDENCE_UNAVAILABLE") from error
        if (
            proposal.id != order.proposal_id or proposal.instrument_id != order.instrument_id
            or proposal.strategy_id != order.strategy_id or proposal.data_origin != origin
            or proposal.quantity != order.quantity or decision.approved_quantity != order.quantity
            or order.order_type != OrderType.LIMIT or order.price != proposal.entry
            or order.transaction_type != (
                TransactionType.BUY if proposal.direction == SignalDirection.LONG
                else TransactionType.SELL
            )
        ):
            raise SafetyError("PENDING_ENTRY_EVIDENCE_MISMATCH")
        entries.append(PendingEntry(
            proposal_id=order.proposal_id,
            strategy_id=order.strategy_id,
            exposure=Exposure(
                instrument_id=proposal.instrument_id, sector=proposal.sector,
                underlying=proposal.underlying,
                notional=remaining * proposal.exposure_per_unit,
            ),
            margin=remaining * (proposal.margin_per_unit + proposal.risk_cost_per_unit),
            risk=remaining * proposal.planned_risk_per_unit,
        ))
    return entries
