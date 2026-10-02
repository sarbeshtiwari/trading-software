"""Durable dashboard authentication, independent of broker credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0006_dashboard_auth"
down_revision = "0005_fundamental_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "login_guards",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer, nullable=False),
        sa.Column("window_start", sa.BigInteger, nullable=False),
        sa.Column("locked_until", sa.BigInteger, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
    )
    op.create_table(
        "dashboard_sessions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("refresh_hash", sa.String(64), nullable=False),
        sa.Column("previous_refresh_hash", sa.String(64)),
        sa.Column("credential_fingerprint", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
    )


def downgrade():
    op.drop_table("dashboard_sessions")
    op.drop_table("login_guards")
