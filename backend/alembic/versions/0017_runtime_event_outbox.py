"""Transactional OMS event publication intents."""

import sqlalchemy as sa
from alembic import op

revision = "0017_runtime_event_outbox"
down_revision = "0016_candle_time_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_event_outbox",
        sa.Column("audit_id", sa.String(40), sa.ForeignKey("audit_events.id"), primary_key=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_runtime_event_pending", "runtime_event_outbox", ["published_at"])


def downgrade():
    op.drop_index("ix_runtime_event_pending", table_name="runtime_event_outbox")
    op.drop_table("runtime_event_outbox")
