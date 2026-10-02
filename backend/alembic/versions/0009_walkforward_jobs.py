"""Audited walk-forward experiment reservations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_walkforward_jobs"
down_revision = "0008_historical_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "walkforward_jobs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("slot", sa.String(16), nullable=True, unique=True),
        sa.Column("owner_id", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column(
            "specification",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "frozen_inputs",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("progress_pct", sa.Numeric(12, 6), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("walkforward_jobs")
