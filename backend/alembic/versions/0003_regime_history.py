"""Regime history, without replacing existing tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_regime_history"
down_revision = "0002_paper_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regime_history",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("underlying", sa.String(64), nullable=False),
        sa.Column("data_origin", sa.String(16), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("underlying", "data_origin", "ts", name="regime_history_identity"),
    )
    op.create_index("ix_regime_history_underlying_ts", "regime_history", ["underlying", "ts"])
    op.create_index("ix_regime_history_created_at", "regime_history", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_regime_history_created_at", table_name="regime_history")
    op.drop_index("ix_regime_history_underlying_ts", table_name="regime_history")
    op.drop_table("regime_history")
