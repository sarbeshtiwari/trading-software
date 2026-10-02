"""Versioned risk configuration and strategy registrations — DB-017, STRAT-002,
STRAT-010, STRAT-011, RISK-016.

Risk limits are versioned rather than mutated. Every change writes a new row with
an author and a reason, and the previous version stays queryable — so a past
decision can be replayed against the limits that were actually in force.

Strategy registrations hold the LIVE-approval gate. Approval is granted only when
backtest, out-of-sample and paper evidence meet the configured thresholds, and a
parameter change revokes it automatically (STRAT-011).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, RATIO, Base, TimestampMixin

__all__ = ["RiskConfigVersion", "StrategyRegistration"]


class RiskConfigVersion(TimestampMixin, Base):
    """One immutable row per risk-limit change."""

    __tablename__ = "risk_config_versions"
    __table_args__ = (
        sa.UniqueConstraint("version", name="risk_config_version_unique"),
        sa.Index("ix_risk_config_active", "is_active"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("rcv"))
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    per_trade_risk_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    daily_loss_limit_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    max_gross_exposure_multiple: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    max_concurrent_positions: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    max_trades_per_day: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    min_reward_risk_ratio: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    margin_buffer_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    max_slippage_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)

    #: Absolute-currency forms; the stricter of absolute/percentage binds (RISK-019).
    max_daily_loss_amount: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    max_per_trade_risk_amount: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    max_deployable_capital: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)

    allow_naked_short_options: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    allow_positional: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    #: Greek limits and any additional rule parameters (FNOR-002).
    extra_limits: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    author: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="system")
    reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class StrategyRegistration(TimestampMixin, Base):
    """Per-strategy enablement and the LIVE-approval gate."""

    __tablename__ = "strategy_registrations"
    __table_args__ = (
        sa.UniqueConstraint("strategy_id", "version", name="strategy_registration_unique"),
        sa.Index("ix_strategy_registrations_strategy", "strategy_id"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("str"))
    strategy_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="1.0.0")
    #: Hash of the strategy's parameters; a change revokes LIVE approval.
    parameter_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    enabled_paper: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    enabled_supervised: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    enabled_live: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # --- Evidence gate (STRAT-010) ----------------------------------------
    live_approved: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    approval_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    backtest_run_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    walkforward_run_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    paper_sessions: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    paper_trades: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    oos_trades: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    evidence_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    # --- Degradation (STRAT-012, LEARN-007) -------------------------------
    auto_disabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    auto_disabled_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    auto_disabled_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
