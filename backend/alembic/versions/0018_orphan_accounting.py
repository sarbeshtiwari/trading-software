"""Allow explicitly unavailable accounting for discovered broker positions."""

import sqlalchemy as sa
from alembic import op

revision = "0018_orphan_accounting"
down_revision = "0017_runtime_event_outbox"
branch_labels = None
depends_on = None

FIELDS = ("realised_pnl", "unrealised_pnl", "total_charges")


def upgrade():
    for field in FIELDS:
        op.alter_column("positions", field, existing_type=sa.Numeric(20, 2), nullable=True)


def downgrade():
    missing = op.get_bind().execute(sa.text(
        "SELECT id FROM positions WHERE realised_pnl IS NULL "
        "OR unrealised_pnl IS NULL OR total_charges IS NULL LIMIT 1"
    )).first()
    if missing:
        raise RuntimeError("Cannot downgrade while position accounting is unavailable")
    for field in FIELDS:
        op.alter_column("positions", field, existing_type=sa.Numeric(20, 2), nullable=False)
