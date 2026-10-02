"""Append-only journal records and explicit correction lineage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_journal_revisions"
down_revision = "0009_walkforward_jobs"
branch_labels = None
depends_on = None
TABLES = ("journal_entries", "journal_annotations", "journal_revisions")


def upgrade():
    op.create_table(
        "journal_revisions",
        sa.Column("entry_id", sa.String(40), sa.ForeignKey("journal_entries.id"), primary_key=True),
        sa.Column(
            "previous_id",
            sa.String(40),
            sa.ForeignKey("journal_entries.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("root_id", sa.String(40), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "changes", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("root_id", "version", name="journal_root_version"),
    )
    op.create_index("ix_journal_revisions_root_id", "journal_revisions", ["root_id"])
    for table in TABLES:
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION ats_refuse_mutation();"
            )
        elif op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'journal records are append-only'); END;"
                )


def downgrade():
    for table in reversed(TABLES):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table};")
        elif op.get_bind().dialect.name == "sqlite":
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_{action};")
    op.drop_index("ix_journal_revisions_root_id", "journal_revisions")
    op.drop_table("journal_revisions")
