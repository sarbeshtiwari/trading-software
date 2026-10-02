"""Persisted regime decisions, including complete hysteresis state and input evidence."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, JSONColumn, TimestampMixin


class RegimeHistory(TimestampMixin, Base):
    __tablename__ = "regime_history"
    __table_args__ = (
        sa.UniqueConstraint("underlying", "data_origin", "ts", name="regime_history_identity"),
        sa.Index("ix_regime_history_underlying_ts", "underlying", "ts"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("reg"))
    underlying: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    data_origin: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
