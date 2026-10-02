"""Immutable equity evidence snapshots for point-in-time analysis."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_equity_evidence"
down_revision = "0003_regime_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equity_evidence",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.UniqueConstraint("key", "origin", "known_at", name="equity_evidence_identity"),
    )


def downgrade() -> None:
    op.drop_table("equity_evidence")
