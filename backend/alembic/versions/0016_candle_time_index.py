"""Represent the Timescale candle time index explicitly in the managed schema."""

import sqlalchemy as sa
from alembic import op

revision = "0016_candle_time_index"
down_revision = "0015_serialized_fill_limits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("candles_ts_idx", "candles", [sa.text("ts DESC")], if_not_exists=True)


def downgrade():
    """Retain the pre-existing Timescale index rather than deleting source-era state."""
