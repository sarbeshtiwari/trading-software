"""Proposals, risk decisions and sizing records — DB-010, DB-011, SIZE-008.

These three tables answer the two questions the system must always be able to
answer (contract §13):

* *Why did you take this trade?* — proposal → risk decision (APPROVED) → orders.
* *Why did you NOT take this trade?* — proposal or considered candidate → risk
  decision (REJECTED) with the binding rule and the numbers it used.

``rules_evaluated`` stores every rule with its inputs, not only the one that
failed, so a decision can be replayed and verified later (RISK-015).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import MarketRegime, Product, Segment, SignalDirection
from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, PRICE, RATIO, Base, TimestampMixin
from app.modes import TradingMode

__all__ = ["Proposal", "RiskDecision", "SizingRecord", "ConsideredCandidate"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class Proposal(TimestampMixin, Base):
    """A structured trade proposal awaiting deterministic validation and risk."""

    __tablename__ = "proposals"
    __table_args__ = (
        sa.Index("ix_proposals_created", "created_at"),
        sa.Index("ix_proposals_strategy", "strategy_id"),
        sa.Index("ix_proposals_status", "status"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("prp"))

    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    segment: Mapped[Segment] = mapped_column(_enum(Segment, "segment"), nullable=False)
    product: Mapped[Product] = mapped_column(_enum(Product, "product"), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(
        _enum(SignalDirection, "signal_direction"), nullable=False
    )

    strategy_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="1.0.0")

    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    target_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    #: The LLM's suggestion. Advisory only — the sizer decides (AID-004).
    suggested_quantity: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    #: What the deterministic sizer actually produced.
    approved_quantity: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    confidence: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=0)
    thesis: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    invalidation_conditions: Mapped[list[Any]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    #: PROPOSED | VALIDATED | REJECTED_VALIDATION | RISK_APPROVED | RISK_REJECTED
    #: | AWAITING_APPROVAL | EXPIRED_UNAPPROVED | EXECUTED | ABANDONED
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="PROPOSED")
    rejection_code: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    rejection_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    # --- Provenance of the reasoning --------------------------------------
    #: Whether this came from the quantitative path or the LLM path (AID-008).
    origin: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="QUANT")
    llm_call_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    regime: Mapped[Optional[MarketRegime]] = mapped_column(
        _enum(MarketRegime, "market_regime"), nullable=True
    )
    #: Immutable snapshot of every input used (AUDIT-004).
    context_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    @property
    def risk_per_unit(self) -> Decimal:
        """Absolute distance between entry and stop."""
        return abs(self.entry_price - self.stop_loss)


class RiskDecision(TimestampMixin, Base):
    """The deterministic risk engine's verdict (DB-011)."""

    __tablename__ = "risk_decisions"
    __table_args__ = (
        sa.Index("ix_risk_decisions_proposal", "proposal_id"),
        sa.Index("ix_risk_decisions_created", "created_at"),
        sa.Index("ix_risk_decisions_approved", "approved"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("dec"))
    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)

    approved: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    #: The first rule that failed. Null when approved.
    binding_rule: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    rejection_code: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    #: Every rule evaluated, with its inputs and computed values.
    rules_evaluated: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    #: Portfolio/market state the decision was made against.
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: The exact limit set in force, by version.
    risk_config_version: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    approved_quantity: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    risk_amount: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)

    #: Set when this is the pre-execution re-check rather than the initial verdict.
    is_preflight: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SizingRecord(TimestampMixin, Base):
    """How a quantity was arrived at (SIZE-008). Replayable."""

    __tablename__ = "sizing_records"
    __table_args__ = (sa.Index("ix_sizing_proposal", "proposal_id"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("siz"))
    proposal_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)

    method: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    formula_version: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="1.0.0")

    capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk_budget: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    raw_quantity: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    lot_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    final_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    #: Which cap actually bound: risk, margin, exposure, daily budget, or none.
    binding_constraint: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    #: Set when the result was zero — an explicit NO TRADE, not a silent minimum.
    zero_reason: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)


class ConsideredCandidate(TimestampMixin, Base):
    """An instrument that was evaluated but never became a proposal (AID-009).

    Without this, "why did you not trade X today" is unanswerable: the absence of
    a proposal is not evidence of a decision.
    """

    __tablename__ = "considered_candidates"
    __table_args__ = (
        sa.Index("ix_considered_cycle", "cycle_id"),
        sa.Index("ix_considered_instrument_created", "instrument_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("cnd"))
    cycle_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    instrument_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    strategy_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    #: Stage at which it stopped: UNIVERSE | REGIME | SIGNAL | VALIDATION | RISK | SIZING
    stopped_at_stage: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reason_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    inputs: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
