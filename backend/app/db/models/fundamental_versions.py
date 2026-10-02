"""Intraday-versioned fundamentals; legacy date-only snapshots remain untouched."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONColumn


class FundamentalVersion(Base):
    __tablename__ = "fundamental_versions"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_id", "source", "known_at", name="fundamental_version_identity"
        ),
    )
    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    known_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)


class CorporateCalendarVersion(Base):
    __tablename__ = "corporate_calendar_versions"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_id", "source", "known_at", name="corporate_calendar_version_identity"
        ),
    )
    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    known_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
