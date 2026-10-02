"""Committed OMS event publication intent; broker orders never originate here."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RuntimeEventOutbox(Base):
    __tablename__ = "runtime_event_outbox"
    __table_args__ = (sa.Index("ix_runtime_event_pending", "published_at"),)

    audit_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("audit_events.id"), primary_key=True
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
