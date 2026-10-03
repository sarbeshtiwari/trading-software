"""Real PostgreSQL adoption and lossless migration refusal in temporary tables."""

import importlib.util
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.models import BrokerPosition
from app.core.enums import Exchange, InstrumentType, Product, Segment
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.trading import Position
from app.execution import orphan_review, orphans
from tests.conftest import BACKEND_ROOT

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set ATS_TEST_POSTGRES_URL")


def migration(connection, action):
    spec = importlib.util.spec_from_file_location(
        "orphan_migration", BACKEND_ROOT / "alembic/versions/0018_orphan_accounting.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, action)()


async def test_postgres_adoption_and_no_lossy_downgrade(fake_clock, monkeypatch):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in (
                    "positions", "orders", "instruments", "audit_events", "runtime_event_outbox",
                    "discrepancies", "system_state",
                ):
                    await connection.execute(sa.text(
                        f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"
                    ))
                await connection.run_sync(migration, "upgrade")
                monkeypatch.setattr(db_session, "_sessionmaker", async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                ))
                async with db_session.session_scope() as session:
                    session.add(Instrument(
                        exchange=Exchange.NSE, segment=Segment.CASH, trading_symbol="TEST_ORPHAN",
                        instrument_type=InstrumentType.EQUITY,
                    ))
                remote = BrokerPosition(
                    trading_symbol="TEST_ORPHAN", exchange=Exchange.NSE, segment=Segment.CASH,
                    product=Product.MIS, net_quantity=-10, average_price=Decimal("100"),
                )
                async with db_session.session_scope() as session:
                    assert await orphans.discover(session, [remote], fake_clock)
                async with db_session.session_scope() as session:
                    position = await session.scalar(sa.select(Position))
                    assert position.net_quantity == -10 and position.realised_pnl is None
                    assert position.total_charges is None and position.net_pnl is None
                    assert not await orphans.discover(session, [remote], fake_clock)
                    review = await orphan_review.inspect(session, position, fake_clock.utcnow(), "owner")
                    identifier = position.id
                acknowledged = await orphan_review.acknowledge(
                    identifier, actor="owner", reason="Reviewed isolated PostgreSQL evidence",
                    expected_head=review.head_hash, clock=fake_clock,
                )
                assert acknowledged.acknowledged and acknowledged.acknowledged_by == "owner"
                async with db_session.session_scope() as session:
                    position = await session.get(Position, identifier)
                    assert (await orphan_review.inspect(session, position, fake_clock.utcnow(), "owner")).acknowledged
                    assert position.total_charges is None and position.net_pnl is None
                with pytest.raises(RuntimeError, match="accounting is unavailable"):
                    await connection.run_sync(migration, "downgrade")
                assert await connection.scalar(sa.text("SELECT count(*) FROM positions")) == 1
                await connection.execute(sa.text("DELETE FROM pg_temp.positions"))
                await connection.run_sync(migration, "downgrade")
                await connection.run_sync(migration, "upgrade")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
