"""System state, health history, heartbeats and discrepancies — DB-018,
MON-007, MON-009, REC-003, EMG-001, LIVE-004.

``system_state`` is a single row (id = ``singleton``) holding the durable safety
state: kill switch, armed flag, trading-enabled flag, daily counters and the last
successful reconciliation. It is read during recovery, which is why these facts
live in the database rather than in memory or Redis.

The armed flag is deliberately stored *with* the session date it was granted for:
the system must never come up armed after a restart (LIVE-004), and a stored
boolean alone would do exactly that.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import HealthStatus
from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, Base, TimestampMixin
from app.modes import TradingMode

__all__ = ["SystemState", "HealthRecord", "Heartbeat", "Discrepancy", "PortfolioSnapshot"]

SINGLETON_ID = "singleton"


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class SystemState(TimestampMixin, Base):
    """Durable safety state. Exactly one row."""

    __tablename__ = "system_state"

    id: Mapped[str] = mapped_column(sa.String(16), primary_key=True, default=SINGLETON_ID)

    mode: Mapped[TradingMode] = mapped_column(
        _enum(TradingMode, "trading_mode"), nullable=False, default=TradingMode.PAPER
    )

    # --- Safety flags -----------------------------------------------------
    trading_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    trading_disabled_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    new_entries_blocked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    new_entries_blocked_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    kill_switch_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    kill_switch_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    kill_switch_activated_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    kill_switch_actor: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    # --- Arming (LIVE-003, LIVE-004) --------------------------------------
    armed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    #: The session the arming was granted for. Arming never carries to another day.
    armed_for_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    armed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    armed_by: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    #: Process identity that armed; a different process must re-arm.
    armed_process_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    # --- Daily counters (reset at session start) --------------------------
    session_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    trades_today: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    realised_pnl_today: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    orders_submitted_today: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    token_requests_today: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    llm_cost_today_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 6), nullable=False, default=0
    )

    # --- Equity tracking (RISK-006) ---------------------------------------
    starting_capital: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    current_equity: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    peak_equity: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)

    # --- Recovery ---------------------------------------------------------
    last_reconciliation_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    open_discrepancies: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_startup_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_shutdown_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    version: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)


class HealthRecord(Base):
    """Health-check result history (MON-009)."""

    __tablename__ = "health_records"
    __table_args__ = (
        sa.Index("ix_health_checked", "checked_at"),
        sa.Index("ix_health_name_checked", "name", "checked_at"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("hlt"))
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[HealthStatus] = mapped_column(_enum(HealthStatus, "health_status"), nullable=False)
    critical: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class Heartbeat(Base):
    """Liveness marker so an external watcher can detect a stalled loop (MON-007)."""

    __tablename__ = "heartbeats"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    process_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)


class Discrepancy(TimestampMixin, Base):
    """A mismatch between local state and broker state (REC-003, REC-004)."""

    __tablename__ = "discrepancies"
    __table_args__ = (sa.Index("ix_discrepancies_open", "resolved"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("dsc"))
    #: POSITION | ORDER | ORPHAN_POSITION | UNPROTECTED_POSITION | MARGIN
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    instrument_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    trading_symbol: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    local_state: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    broker_state: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    delta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    resolved: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    resolution: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class PortfolioSnapshot(Base):
    """Periodic portfolio state for history and reporting (PORT-007)."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (sa.Index("ix_portfolio_snapshots_ts", "ts"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("psn"))
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)

    equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    realised_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    unrealised_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    charges: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    gross_exposure: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    net_exposure: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    available_margin: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    used_margin: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    open_positions: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    positions: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    greeks: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
