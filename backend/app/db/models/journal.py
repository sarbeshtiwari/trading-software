"""Trade journal — DB-013, JRN-001…JRN-007.

One row per trade (and one per notable rejection), carrying the full lineage plus
the market context at entry and exit. Core fields are immutable once written;
only annotations may be added afterwards (JRN-007), so the journal cannot be
retroactively tidied to look better than it was.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.core.enums import ExitReason, MarketRegime, SignalDirection
from app.core.ids import new_id
from app.db.base import MONEY, PRICE, RATIO, Base, JSONColumn, TimestampMixin
from app.modes import TradingMode

__all__ = ["JournalAnnotation", "JournalEntry", "JournalRevision"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class JournalEntry(TimestampMixin, Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        sa.Index("ix_journal_opened", "opened_at"),
        sa.Index("ix_journal_strategy", "strategy_id"),
        sa.Index("ix_journal_instrument", "instrument_id"),
        sa.Index("ix_journal_kind", "kind"),
        sa.Index("ix_journal_mode", "mode"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("jrn"))

    #: ``TRADE`` for an executed trade, ``REJECTION`` for a proposal that was
    #: refused (JRN-004) — both belong in the journal.
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="TRADE")

    # --- Lineage (DB-013) -------------------------------------------------
    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    risk_decision_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    position_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)
    entry_order_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    exit_order_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    audit_chain_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)

    instrument_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    trading_symbol: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    strategy_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    strategy_version: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    direction: Mapped[Optional[SignalDirection]] = mapped_column(
        _enum(SignalDirection, "signal_direction"), nullable=True
    )

    # --- Entry ------------------------------------------------------------
    planned_entry: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    actual_entry: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    planned_stop: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    planned_target: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    risk_amount: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    opened_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # --- Exit -------------------------------------------------------------
    actual_exit: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    exit_reason: Mapped[Optional[ExitReason]] = mapped_column(
        _enum(ExitReason, "exit_reason"), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    holding_period_seconds: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    invalidation_fired: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)

    # --- Outcome ----------------------------------------------------------
    gross_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    charges: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    net_pnl: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    #: Net P&L expressed in units of the risk taken.
    r_multiple: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    entry_slippage: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    exit_slippage: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)

    # --- Context (JRN-002) ------------------------------------------------
    regime_at_entry: Mapped[Optional[MarketRegime]] = mapped_column(
        _enum(MarketRegime, "market_regime"), nullable=True
    )
    indicator_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    chain_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    news_used: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    ai_thesis: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    ai_confidence: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    plan_adherence: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)

    # --- Rejection detail (JRN-004) ---------------------------------------
    rejection_code: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    rejection_rule: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    rejection_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    #: Set when a correction supersedes this entry (JRN-007).
    superseded_by: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    annotations: Mapped[list["JournalAnnotation"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", lazy="selectin"
    )


class JournalAnnotation(TimestampMixin, Base):
    """Owner notes and tags — the only mutable part of the journal (JRN-005)."""

    __tablename__ = "journal_annotations"

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("jan"))
    journal_entry_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    tags: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    author: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="owner")

    entry: Mapped[JournalEntry] = relationship(back_populates="annotations")


class JournalRevision(Base):
    """Append-only version linkage; original economic records are never rewritten."""

    __tablename__ = "journal_revisions"
    __table_args__ = (sa.UniqueConstraint("root_id", "version", name="journal_root_version"),)

    entry_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("journal_entries.id"), primary_key=True
    )
    previous_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("journal_entries.id"), nullable=False, unique=True
    )
    root_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("journal_entries.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


@sa.event.listens_for(Session, "before_flush")
def refuse_journal_changes(session, flush_context, instances):
    for row in session.deleted:
        if isinstance(row, (JournalEntry, JournalAnnotation, JournalRevision)):
            raise ValueError("journal records are append-only")
    for row in session.dirty:
        if isinstance(
            row, (JournalEntry, JournalAnnotation, JournalRevision)
        ) and session.is_modified(row, include_collections=False):
            raise ValueError("journal records are append-only; create a versioned correction")


@sa.event.listens_for(Session, "do_orm_execute")
def refuse_journal_bulk_changes(state):
    if (state.is_update or state.is_delete) and getattr(
        getattr(state.statement, "table", None), "name", None
    ) in {"journal_entries", "journal_annotations", "journal_revisions"}:
        raise ValueError("journal bulk mutations are forbidden")
