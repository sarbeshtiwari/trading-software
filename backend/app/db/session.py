"""Async engine and session lifecycle — DB-002.

One engine per process, one session per unit of work. Sessions are never shared
between tasks: a trading loop and an HTTP request touching the same session would
interleave transactions unpredictably.

The engine is created lazily so that importing the module (which tests and
Alembic both do) never opens a connection.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.core.errors import ConnectionFailedError
from app.core.logging import get_logger

logger = get_logger("db.session")

__all__ = [
    "init_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
    "get_session",
    "dispose_engine",
    "ping",
]

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _engine_kwargs(settings: Settings) -> dict[str, object]:
    url = settings.database_url
    # SQLite (used for fast model-level tests) has no meaningful pool settings.
    if url.startswith("sqlite"):
        return {"echo": settings.database_echo}
    return {
        "echo": settings.database_echo,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_pre_ping": True,  # a stale connection after a DB restart must not surface as an error
        "pool_recycle": 1800,
    }


def init_engine(settings: Optional[Settings] = None, *, force: bool = False) -> AsyncEngine:
    """Create the process engine and session factory."""
    global _engine, _sessionmaker
    if _engine is not None and not force:
        return _engine

    resolved = settings or get_settings()
    if _engine is not None and force:
        logger.warning("Re-initialising the database engine")

    _engine = create_async_engine(resolved.database_url, **_engine_kwargs(resolved))
    _sessionmaker = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,  # objects stay usable after commit, inside one request
        autoflush=False,
    )
    logger.info(
        "Database engine initialised",
        extra={"dialect": _engine.dialect.name, "pool": type(_engine.pool).__name__},
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None  # noqa: S101 - established by init_engine
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """Close all pooled connections (shutdown hook)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _sessionmaker = None


async def ping() -> None:
    """Verify connectivity. Raises :class:`ConnectionFailedError` when unreachable."""
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - normalised into our taxonomy
        raise ConnectionFailedError(
            f"Database is not reachable: {exc}",
            context={"error": str(exc)},
        ) from exc
