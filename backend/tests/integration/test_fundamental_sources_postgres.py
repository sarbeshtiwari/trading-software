"""Bounded source discovery against real PostgreSQL, with rollback-only test rows."""

import os
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.fundamentals import sources
from app.core.clock import UTC
from app.db import session as db_session
from app.db.models.fundamental_versions import FundamentalVersion

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")


async def test_source_pagination_and_receipt_cutoff_postgres(monkeypatch, fake_clock):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    sa.text(
                        "CREATE TEMP TABLE fundamental_versions "
                        "(LIKE public.fundamental_versions INCLUDING ALL) ON COMMIT DROP"
                    )
                )
                monkeypatch.setattr(
                    db_session,
                    "_sessionmaker",
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    ),
                )
                now = fake_clock.now().astimezone(UTC)
                async with db_session.session_scope() as session:
                    session.add_all(
                        [
                            FundamentalVersion(
                                id=f"source-{index}",
                                instrument_id="isolated",
                                source=f"source-{index:03}",
                                known_at=now - timedelta(days=1),
                                received_at=now,
                                payload={},
                            )
                            for index in range(51)
                        ]
                    )
                first = await sources("isolated", as_of=now, offset=0)
                assert len(first.sources) == 50 and first.has_more
                last = await sources("isolated", as_of=now, offset=50)
                assert [row.source for row in last.sources] == ["source-050"]
                assert not last.has_more
                assert first.sources[0].received_at == now
                before = await sources("isolated", as_of=now - timedelta(seconds=1), offset=0)
                assert before.sources == ()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
