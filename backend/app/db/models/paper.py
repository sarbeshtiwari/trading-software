"""Paper-broker persistence — PAPER-005.

The paper broker holds the state a real broker would hold on its side: its own
orders, fills and positions. That is not the same thing as the system record in
``orders``/``positions``, so it lives in its own row rather than being mixed in.

Keeping it separate also makes the reconciliation path identical in both modes:
local records are compared against "what the broker says", whoever the broker is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONColumn, Base, TimestampMixin
from app.modes import TradingMode

__all__ = ["PaperBrokerState", "PAPER_STATE_ID"]

PAPER_STATE_ID = "paper"


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class PaperBrokerState(TimestampMixin, Base):
    """Single-row snapshot of the simulated broker."""

    __tablename__ = "paper_broker_state"

    id: Mapped[str] = mapped_column(sa.String(16), primary_key=True, default=PAPER_STATE_ID)
    mode: Mapped[TradingMode] = mapped_column(
        _enum(TradingMode, "trading_mode"), nullable=False, default=TradingMode.PAPER
    )

    #: Serialised PaperAccount (cash, margin, positions).
    account: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: Simulated orders keyed by broker order id.
    orders: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: Simulated fills, in execution order.
    trades: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    #: reference_id -> broker order id, the idempotency index.
    reference_index: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    session_date: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    fill_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
