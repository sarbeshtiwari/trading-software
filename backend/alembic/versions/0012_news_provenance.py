"""Explicit news provenance and independent publisher identities; no invented backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0012_news_provenance"
down_revision = "0011_llm_budget"
branch_labels = None
depends_on = None


def upgrade():
    for table, name, length in (
        ("news_sources", "publisher_id", 128),
        ("news_items", "data_origin", 16),
    ):
        existing = {
            column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table)
        }
        if name not in existing:
            op.add_column(table, sa.Column(name, sa.String(length), nullable=True))
        elif (
            not isinstance(existing[name]["type"], sa.String)
            or existing[name]["type"].length != length
            or not existing[name]["nullable"]
        ):
            raise RuntimeError("Incompatible existing news provenance column")


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM news_sources WHERE publisher_id IS NOT NULL")
    ) or op.get_bind().scalar(
        sa.text("SELECT count(*) FROM news_items WHERE data_origin IS NOT NULL")
    ):
        raise RuntimeError("Cannot discard recorded news provenance during downgrade")
    with op.batch_alter_table("news_items") as batch:
        batch.drop_column("data_origin")
    with op.batch_alter_table("news_sources") as batch:
        batch.drop_column("publisher_id")
