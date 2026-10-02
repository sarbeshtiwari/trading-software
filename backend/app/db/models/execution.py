"""Conservative single-lifecycle reservation for the initial PAPER worker."""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperExecutionSlot(Base):
    __tablename__ = "paper_execution_slots"
    id: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(sa.String(40), nullable=False, unique=True)


class PaperStateRevision(Base):
    __tablename__ = "paper_state_revisions"
    id: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
