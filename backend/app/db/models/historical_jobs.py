"""Durable single-slot historical jobs, separate from trading account state."""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RATIO, Base


class HistoricalJob(Base):
    __tablename__ = "historical_jobs"

    id: Mapped[str] = mapped_column(sa.String(26), primary_key=True)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    slot: Mapped[str | None] = mapped_column(sa.String(16), nullable=True, unique=True)
    owner_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    recording_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    progress_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=0)
    published: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
