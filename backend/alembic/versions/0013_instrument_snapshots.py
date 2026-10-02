"""Retain immutable source evidence for instrument master imports."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_instrument_snapshots"
down_revision = "0012_news_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instrument_master_snapshots",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(256), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("decoded_csv", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column(
            "outcome", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER instrument_snapshot_immutable BEFORE UPDATE OR DELETE "
            "ON instrument_master_snapshots FOR EACH ROW EXECUTE FUNCTION ats_refuse_mutation();"
        )
    elif op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER instrument_snapshot_{action.lower()} BEFORE {action} "
                "ON instrument_master_snapshots BEGIN SELECT RAISE(ABORT, 'immutable snapshot'); END;"
            )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM instrument_master_snapshots")):
        raise RuntimeError("Cannot discard instrument source evidence")
    op.drop_table("instrument_master_snapshots")
