"""Backtest and walk-forward persistence — DB-019, BT-007, BT-013, WF-003.

A run row stores everything needed to reproduce it: strategy version, parameters,
data window, fill and cost assumptions, and the random seed. Reproducibility is a
requirement, not a nicety — a backtest that cannot be re-run is not evidence.

``combinations_tested`` is recorded for parameter sweeps so a result selected from
a large search space is visibly selected from a large search space (BT-009).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ExitReason
from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, PRICE, RATIO, Base, TimestampMixin

__all__ = ["BacktestRun", "BacktestResult", "BacktestTrade"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class BacktestRun(TimestampMixin, Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        sa.Index("ix_backtest_runs_strategy", "strategy_id"),
        sa.Index("ix_backtest_runs_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("bkt"))
    #: BACKTEST | WALKFORWARD | SWEEP
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="BACKTEST")

    strategy_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    seed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    start_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    end_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    universe: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)

    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Fill/slippage/cost assumptions, stored so results are interpretable later.
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    risk_config_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=True
    )

    #: Data coverage actually available, and whether it met the configured minimum.
    data_days_available: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    data_window_warning: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    survivorship_note: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    combinations_tested: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="PENDING")
    progress_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=0)
    error_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    #: Always true for backtests; enforced so results cannot be shown as live.
    simulated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    results: Mapped[list["BacktestResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )
    trades: Mapped[list["BacktestTrade"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class BacktestResult(TimestampMixin, Base):
    """Metrics for a run, or for one walk-forward window within it."""

    __tablename__ = "backtest_results"
    __table_args__ = (sa.Index("ix_backtest_results_run", "run_id"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("bkr"))
    run_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )

    #: FULL | IN_SAMPLE | OUT_OF_SAMPLE
    window_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="FULL")
    window_index: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    window_start: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    window_end: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    window_parameters: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    total_return: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    cagr: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    sharpe: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    sortino: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    win_rate: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    expectancy: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    trade_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    exposure_time_pct: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    avg_holding_seconds: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    gross_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    total_charges: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    net_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)

    #: ``[[iso_date, equity], ...]``
    equity_curve: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    drawdown_curve: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    rejections: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    overfitting_flag: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    run: Mapped[BacktestRun] = relationship(back_populates="results")


class BacktestTrade(Base):
    """One simulated trade produced by a run."""

    __tablename__ = "backtest_trades"
    __table_args__ = (sa.Index("ix_backtest_trades_run", "run_id"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("btt"))
    run_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    window_index: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    exit_ts: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    exit_reason: Mapped[Optional[ExitReason]] = mapped_column(
        _enum(ExitReason, "exit_reason"), nullable=True
    )
    stop_loss: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    target: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    gross_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    charges: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    net_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    r_multiple: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)

    run: Mapped[BacktestRun] = relationship(back_populates="trades")
