"""Historical equity evidence without mutating the live instrument master."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONColumn


class EquityEvidence(Base):
    __tablename__ = "equity_evidence"
    __table_args__ = (
        sa.UniqueConstraint("key", "origin", "known_at", name="equity_evidence_identity"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    origin: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    known_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
