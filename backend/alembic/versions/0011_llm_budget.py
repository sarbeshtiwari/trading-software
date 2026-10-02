"""Durable advisory reservations and unavailable remote costs."""

import sqlalchemy as sa
from alembic import op

revision = "0011_llm_budget"
down_revision = "0010_journal_revisions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("llm_calls") as batch:
        batch.alter_column("cost_usd", existing_type=sa.Numeric(12, 6), nullable=True)
    op.create_table(
        "llm_budget_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("spent_microusd", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("spent_tokens", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "llm_provider_states",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("open_until", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_call_id", sa.String(40)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM llm_calls WHERE cost_usd IS NULL")):
        raise RuntimeError("Cannot discard unavailable LLM costs during downgrade")
    op.drop_table("llm_provider_states")
    op.drop_table("llm_budget_days")
    with op.batch_alter_table("llm_calls") as batch:
        batch.alter_column("cost_usd", existing_type=sa.Numeric(12, 6), nullable=False)
