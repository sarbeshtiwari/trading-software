"""Real PostgreSQL closed-bar/ingestion cutoffs in rollback-only temporary storage."""

import os
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import session as db_session
from app.marketdata.ingest import CandleStore
from tests.integration.test_marketdata_store import _bars

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")


async def test_postgres_closed_observation_cutoffs(monkeypatch, fake_clock):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    sa.text(
                        "CREATE TEMP TABLE candles (LIKE public.candles INCLUDING ALL) ON COMMIT DROP"
                    )
                )
                monkeypatch.setattr(
                    db_session,
                    "_sessionmaker",
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    ),
                )
                store = CandleStore()
                bars = _bars(3, start=fake_clock.now() - timedelta(minutes=1), interval=1)
                assert (
                    await store.write("isolated-chart", 1, bars, data_origin="SYNTHETIC")
                ).written == 3
                current = await store.closed_observations(
                    "isolated-chart", 1, origin="SYNTHETIC", as_of=fake_clock.now()
                )
                assert len(current) == 1
                assert current[0].ts == bars[0].ts
                assert (
                    await store.closed_observations(
                        "isolated-chart", 1, origin="LIVE", as_of=fake_clock.now()
                    )
                    == []
                )
                assert (
                    await store.closed_observations(
                        "isolated-chart",
                        1,
                        origin="SYNTHETIC",
                        as_of=fake_clock.now() - timedelta(seconds=1),
                    )
                    == []
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
