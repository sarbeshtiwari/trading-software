"""Reserve one PAPER lifecycle durably, including across process restart."""

import sqlalchemy as sa
from alembic import op

revision = "0007_paper_execution_slot"
down_revision = "0006_dashboard_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_state_revisions",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
    )
    op.create_table(
        "paper_execution_slots",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("proposal_id", sa.String(40), nullable=False, unique=True),
    )


def downgrade():
    op.drop_table("paper_execution_slots")
    op.drop_table("paper_state_revisions")
