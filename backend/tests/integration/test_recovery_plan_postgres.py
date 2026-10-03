"""Verify recovery-plan parity on PostgreSQL using temporary copies of test evidence."""

import os
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.validation import _utc
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal, RiskDecision
from app.db.models.instrument import Instrument
from app.db.models.trading import Order, Position, Trade
from app.execution.position_recovery import build
from tests.integration.test_orphan_review import adopted_fixture
from tests.integration.test_paper_execution import credentials
from tests.integration.test_position_recovery_plan import reviewed_orphan

__all__ = ["credentials"]
POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="set ATS_TEST_POSTGRES_URL")
async def test_postgres_plan_preserves_fixture_audit_evidence(
    db_engine, credentials, fake_clock, monkeypatch
):
    _executor, api = await adopted_fixture(credentials, fake_clock)
    engine = create_async_engine(POSTGRES_URL)
    try:
        identifier = await reviewed_orphan(api)
        expected = await build(identifier, fake_clock.utcnow(), "owner")
        tables = [model.__table__ for model in (
            Instrument, Order, Position, Trade, AuditEvent, Proposal, RiskDecision
        )]
        snapshots = {}
        async with db_session.session_scope() as session:
            for table in tables:
                snapshots[table.name] = [
                    {key: _utc(value) if isinstance(value, datetime) else value
                     for key, value in row.items()}
                    for row in (await session.execute(sa.select(table))).mappings()
                ]
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in tables:
                    await connection.execute(sa.text(
                        f"CREATE TEMP TABLE {table.name} (LIKE public.{table.name} INCLUDING ALL) ON COMMIT DROP"
                    ))
                    if snapshots[table.name]:
                        await connection.execute(sa.insert(table), snapshots[table.name])
                monkeypatch.setattr(db_session, "_sessionmaker", async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                ))
                actual = await build(identifier, fake_clock.utcnow(), "owner")
                assert actual == expected
            finally:
                await transaction.rollback()
    finally:
        await api.aclose()
        await engine.dispose()
