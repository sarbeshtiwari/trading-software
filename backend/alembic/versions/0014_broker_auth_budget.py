"""Shared rolling token-attempt budget."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_broker_auth_budget"
down_revision = "0013_instrument_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "broker_auth_budgets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "state", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM broker_auth_budgets")):
        raise RuntimeError("Refusing to discard token attempt history")
    op.drop_table("broker_auth_budgets")
