from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONColumn


class InstrumentMasterSnapshot(Base):
    __tablename__ = "instrument_master_snapshots"

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    content_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    decoded_csv: Mapped[str] = mapped_column(sa.Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    outcome: Mapped[dict] = mapped_column(JSONColumn, nullable=False)
