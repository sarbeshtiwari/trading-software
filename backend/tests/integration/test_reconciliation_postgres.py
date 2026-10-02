"""PAPER discrepancy evidence on PostgreSQL without owner-table mutation."""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import session as db_session
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.system import SINGLETON_ID, Discrepancy, SystemState
from app.execution import discrepancies

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set ATS_TEST_POSTGRES_URL")


async def test_postgres_discrepancy_evidence_sessions_and_atomic_rollback(fake_clock, monkeypatch):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in ("audit_events", "runtime_event_outbox", "discrepancies", "system_state"):
                    await connection.execute(sa.text(
                        f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"
                    ))
                monkeypatch.setattr(db_session, "_sessionmaker", async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                ))
                evidence = {"local": {"quantity": 2}, "broker": {"quantity": 1},
                            "delta": {"quantity": 1}, "clock": fake_clock}
                async with db_session.session_scope() as session:
                    row = await discrepancies.observe(session, **evidence)
                    identifier = row.id
                async with db_session.session_scope() as session:
                    row = await session.get(Discrepancy, identifier)
                    original = (await discrepancies.verify(session, row))[-1].record_hash
                    assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 1
                    await discrepancies.observe(session, **evidence)
                    assert len(await discrepancies.verify(session, row)) == 1

                async def fail(*args, **kwargs):
                    raise RuntimeError("fixture outbox failure")

                with monkeypatch.context() as context:
                    context.setattr(discrepancies, "append", fail)
                    with pytest.raises(RuntimeError, match="outbox failure"):
                        async with db_session.session_scope() as session:
                            await discrepancies.observe(session, **{**evidence, "delta": {"quantity": 9}})
                async with db_session.session_scope() as session:
                    row = await session.get(Discrepancy, identifier)
                    assert row.delta == {"quantity": 1}
                    assert (await discrepancies.verify(session, row))[-1].record_hash == original
                    assert await session.scalar(sa.select(sa.func.count()).select_from(RuntimeEventOutbox)) == 1
                    assert (await discrepancies.pending(session))[0].id == identifier
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
