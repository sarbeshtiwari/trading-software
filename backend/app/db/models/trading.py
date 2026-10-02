"""Orders, fills and positions — DB-007, DB-008, DB-009, OMS-001, OMS-008.

The order row is the durable record of an intent's whole life: the locally
generated intent id, the deterministic broker reference id that makes submission
idempotent (EXEC-004), the broker's own id, every status transition, and the raw
request/response payloads for audit.

Raw payloads are kept deliberately. When a fill is disputed or a rejection is
unexplained, the exact bytes exchanged with the broker are the only evidence that
settles it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.data_origin import ExecutionRealism
from app.core.enums import (
    Exchange,
    ExitReason,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionState,
    Product,
    Segment,
    TransactionType,
    Validity,
)
from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, PRICE, Base, TimestampMixin
from app.modes import TradingMode

__all__ = ["Order", "OrderEvent", "Trade", "Position"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        sa.UniqueConstraint("intent_id", name="orders_intent_id"),
        sa.UniqueConstraint("broker_reference_id", name="orders_broker_reference_id"),
        sa.Index("ix_orders_status", "status"),
        sa.Index("ix_orders_broker_order_id", "broker_order_id"),
        sa.Index("ix_orders_instrument_created", "instrument_id", "created_at"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("filled_quantity >= 0", name="filled_non_negative"),
        sa.CheckConstraint("filled_quantity <= quantity", name="filled_le_quantity"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("ord"))

    #: Locally generated, stable across retries — the idempotency anchor.
    intent_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    #: Derived from ``intent_id``; sent to Groww as ``order_reference_id``.
    broker_reference_id: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    #: Groww's identifier, once known.
    broker_order_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    exchange: Mapped[Exchange] = mapped_column(_enum(Exchange, "exchange"), nullable=False)
    segment: Mapped[Segment] = mapped_column(_enum(Segment, "segment"), nullable=False)
    product: Mapped[Product] = mapped_column(_enum(Product, "product"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType, "order_type"), nullable=False)
    transaction_type: Mapped[TransactionType] = mapped_column(
        _enum(TransactionType, "transaction_type"), nullable=False
    )
    validity: Mapped[Validity] = mapped_column(
        _enum(Validity, "validity"), nullable=False, default=Validity.DAY
    )

    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    trigger_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    filled_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    average_fill_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), nullable=False, default=OrderStatus.CREATED
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    rejection_code: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)

    #: Entry, exit or protective stop — drives position bookkeeping.
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="ENTRY")
    exit_reason: Mapped[Optional[ExitReason]] = mapped_column(
        _enum(ExitReason, "exit_reason"), nullable=True
    )

    # --- Lineage (OMS-008) ------------------------------------------------
    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)
    risk_decision_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    strategy_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True, index=True)
    position_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)
    parent_order_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    # --- Mode and realism (PNL-007, EXEC-014) -----------------------------
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    execution_realism: Mapped[ExecutionRealism] = mapped_column(
        _enum(ExecutionRealism, "execution_realism"), nullable=False
    )

    # --- Compliance (CMP-002) --------------------------------------------
    algo_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    algo_id_absent_reason: Mapped[Optional[str]] = mapped_column(sa.String(128), nullable=True)

    # --- Supervision (SUP-002) -------------------------------------------
    approval_token: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # --- Timestamps -------------------------------------------------------
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    exchange_time: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # --- Raw payloads for audit ------------------------------------------
    request_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    response_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    trades: Mapped[list["Trade"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status.is_open_at_broker


class OrderEvent(Base):
    """One row per state transition (OMS-001). Append-only by convention."""

    __tablename__ = "order_events"
    __table_args__ = (sa.Index("ix_order_events_order_ts", "order_id", "occurred_at"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("oev"))
    order_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[Optional[OrderStatus]] = mapped_column(
        _enum(OrderStatus, "order_status"), nullable=True
    )
    to_status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), nullable=False
    )
    #: Where the transition came from: ``local``, ``poll``, ``feed``, ``recovery``.
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="local")
    detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    order: Mapped[Order] = relationship(back_populates="events")


class Trade(TimestampMixin, Base):
    """An individual fill. One order may have many (EXEC-006)."""

    __tablename__ = "trades"
    __table_args__ = (
        sa.UniqueConstraint("exchange_trade_id", name="trades_exchange_trade_id"),
        sa.Index("ix_trades_order", "order_id"),
        sa.Index("ix_trades_instrument_ts", "instrument_id", "executed_at"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("trd"))
    order_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    position_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)

    #: Null for simulated fills; unique for real ones so a fill is never counted twice.
    exchange_trade_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    transaction_type: Mapped[TransactionType] = mapped_column(
        _enum(TransactionType, "transaction_type"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    #: Transaction costs attributed to this fill (PNL-003).
    brokerage: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    taxes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    other_charges: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    cost_breakdown: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    #: Difference between intended and achieved price (EXEC-011).
    intended_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    slippage: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    execution_realism: Mapped[ExecutionRealism] = mapped_column(
        _enum(ExecutionRealism, "execution_realism"), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="trades")

    @property
    def total_charges(self) -> Decimal:
        return self.brokerage + self.taxes + self.other_charges


class Position(TimestampMixin, Base):
    """Net position in one instrument for one product (DB-009)."""

    __tablename__ = "positions"
    __table_args__ = (
        sa.Index("ix_positions_state", "state"),
        sa.Index("ix_positions_instrument_state", "instrument_id", "state"),
        sa.Index("ix_positions_strategy", "strategy_id"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("pos"))

    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    segment: Mapped[Segment] = mapped_column(_enum(Segment, "segment"), nullable=False)
    product: Mapped[Product] = mapped_column(_enum(Product, "product"), nullable=False)

    side: Mapped[PositionSide] = mapped_column(_enum(PositionSide, "position_side"), nullable=False)
    state: Mapped[PositionState] = mapped_column(
        _enum(PositionState, "position_state"), nullable=False, default=PositionState.OPEN
    )

    #: Signed: positive long, negative short.
    net_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    average_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    #: Running totals, needed for correct FIFO and average-price arithmetic.
    bought_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    sold_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    realised_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    unrealised_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    total_charges: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    last_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    marked_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # --- Protection (EXEC-003, REC-006) -----------------------------------
    stop_loss_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    target_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    trailing_stop_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    protective_order_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    is_protected: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # --- Lineage ----------------------------------------------------------
    strategy_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    regime_at_entry: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)

    opened_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[Optional[ExitReason]] = mapped_column(
        _enum(ExitReason, "exit_reason"), nullable=True
    )

    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    execution_realism: Mapped[ExecutionRealism] = mapped_column(
        _enum(ExecutionRealism, "execution_realism"), nullable=False
    )
    #: Set when the position was discovered at the broker with no local record.
    adopted_from_broker: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    @property
    def is_open(self) -> bool:
        return self.state in (PositionState.OPEN, PositionState.ADOPTED) and self.net_quantity != 0

    @property
    def net_pnl(self) -> Decimal:
        return self.realised_pnl + self.unrealised_pnl - self.total_charges
