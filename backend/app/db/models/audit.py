"""Append-only audit trail — DB-012, AUDIT-001, AUDIT-006, AUDIT-007.

Every field named in contract §13 has a home here. The table is append-only:
the initial migration installs triggers that reject UPDATE and DELETE, so
tampering fails at the database rather than relying on application discipline.

Each row also carries a hash chained to the previous row (AUDIT-007). Altering
any stored row breaks verification of every row after it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Severity
from app.core.ids import new_id
from app.db.base import JSONColumn, Base
from app.modes import TradingMode

__all__ = ["AuditEvent", "ConfigChange"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        sa.Index("ix_audit_occurred", "occurred_at"),
        sa.Index("ix_audit_chain", "chain_id", "sequence"),
        sa.Index("ix_audit_event_type", "event_type"),
        sa.Index("ix_audit_instrument", "instrument_id"),
        sa.UniqueConstraint("chain_id", "sequence", name="audit_chain_sequence"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("aud"))

    #: Groups every record belonging to one decision/trade lineage.
    chain_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    #: Position within the chain, starting at 1.
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        _enum(Severity, "severity"), nullable=False, default=Severity.INFO
    )

    # --- Contract §13 fields ----------------------------------------------
    market_state: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    data_used: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    news_used: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)
    indicators: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    strategy_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    signal: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    risk_calculation: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    position_size: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    decision: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    risk_verdict: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    order_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    broker_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    exit_detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    # --- Linkage ----------------------------------------------------------
    instrument_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    risk_decision_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    position_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    llm_call_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="system")

    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)

    # --- Integrity (AUDIT-007) --------------------------------------------
    previous_hash: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    record_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class ConfigChange(Base):
    """Audited configuration mutation — risk limits, arming, mode, strategies."""

    __tablename__ = "config_changes"
    __table_args__ = (
        sa.Index("ix_config_changes_occurred", "occurred_at"),
        sa.Index("ix_config_changes_target", "target"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("cfg"))
    #: e.g. ``risk_config``, ``strategy_registration``, ``arming``, ``kill_switch``.
    target: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    before: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    after: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
