"""Preserve same-day fundamental revisions without modifying the baseline schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_fundamental_versions"
down_revision = "0004_equity_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_calendar_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("instrument_id", sa.String(40), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.UniqueConstraint(
            "instrument_id", "source", "known_at", name="corporate_calendar_version_identity"
        ),
    )
    op.create_table(
        "fundamental_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("instrument_id", sa.String(40), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.UniqueConstraint(
            "instrument_id", "source", "known_at", name="fundamental_version_identity"
        ),
    )


def downgrade() -> None:
    op.drop_table("fundamental_versions")
    op.drop_table("corporate_calendar_versions")
