"""Actual PostgreSQL rollback and Redis cursor acceptance without owner-table writes."""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import session as db_session
from tests.integration.test_auth import credentials
from tests.integration.test_event_failure_publication import (
    test_atomic_cursor_notifications_restart_and_tamper as verify_publication,
)
from tests.integration.test_redis_events import URL, redis_events

__all__ = ["credentials", "redis_events"]
POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not URL or not POSTGRES_URL, reason="Redis/PostgreSQL URLs required"
)


async def test_postgres_atomic_event_publication(
    credentials, redis_events, fake_clock, monkeypatch
):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    sa.text(
                        "CREATE TEMP TABLE audit_events (LIKE public.audit_events INCLUDING ALL) ON COMMIT DROP"
                    )
                )
                monkeypatch.setattr(
                    db_session,
                    "_sessionmaker",
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    ),
                )
                await verify_publication(None, credentials, redis_events, fake_clock, monkeypatch)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
