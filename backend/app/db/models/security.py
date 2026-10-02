"""Durable single-owner login throttling and revocable sessions."""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginGuard(Base):
    __tablename__ = "login_guards"
    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    window_start: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    locked_until: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class DashboardSession(Base):
    __tablename__ = "dashboard_sessions"
    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    refresh_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    previous_refresh_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    credential_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    expires_at: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    revoked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
