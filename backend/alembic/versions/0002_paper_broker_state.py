"""Paper broker state table.

Revision ID: 0002_paper_state
Revises: 0001_initial
Create Date: 2026-09-18

Added in P1 with the paper broker (PAPER-005). Written as an explicit
``create_table`` rather than another metadata sync: from here on, every schema
change is an ordinary incremental migration so an existing database can be
upgraded without being rebuilt.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_paper_state"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(JSONB, "postgresql")

TRADING_MODE = sa.Enum(
    "PAPER", "SUPERVISED", "LIVE", name="trading_mode", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "paper_broker_state",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("mode", TRADING_MODE, nullable=False),
        sa.Column("account", JSON_TYPE, nullable=False),
        sa.Column("orders", JSON_TYPE, nullable=False),
        sa.Column("trades", JSON_TYPE, nullable=False),
        sa.Column("reference_index", JSON_TYPE, nullable=False),
        sa.Column("session_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fill_config", JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_paper_broker_state"),
    )
    op.create_index(
        "ix_paper_broker_state_created_at", "paper_broker_state", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_paper_broker_state_created_at", table_name="paper_broker_state")
    op.drop_table("paper_broker_state")
