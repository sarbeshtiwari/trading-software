import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.groww.budget import TokenAttemptBudget
from app.core.errors import RateLimitError
from app.db import session as db_session
from app.db.models.broker_auth_budget import BrokerAuthBudget

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")


async def test_postgres_independent_connections_share_attempt_limit(monkeypatch, fake_clock):
    schema = "ats_budget_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        POSTGRES_URL, execution_options={"schema_translate_map": {None: schema}}
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(BrokerAuthBudget.__table__.create)
        monkeypatch.setattr(
            db_session, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
        )
        outcomes = await asyncio.gather(
            *(TokenAttemptBudget(fake_clock).reserve(2) for _ in range(8)), return_exceptions=True
        )
        assert outcomes.count(None) == 2
        assert all(outcome is None or isinstance(outcome, RateLimitError) for outcome in outcomes)
        with pytest.raises(RateLimitError):
            await TokenAttemptBudget(fake_clock).reserve(2)
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
