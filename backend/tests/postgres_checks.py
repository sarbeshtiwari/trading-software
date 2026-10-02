"""Real deployed guarantees, with rollback-only audit rows and isolated fill tables."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models.audit import AuditEvent
from app.db.models.trading import Order, Trade
from tests.conftest import BACKEND_ROOT


async def audit_append_only(url):
    engine = create_async_engine(url)
    identifier = "pgtest_" + uuid4().hex
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    sa.insert(AuditEvent).values(
                        id=identifier,
                        chain_id=identifier,
                        sequence=1,
                        event_type="ISOLATED_ROLLBACK_TEST",
                        actor="postgres_test",
                        occurred_at=datetime.now(timezone.utc),
                        mode="PAPER",
                        record_hash="0" * 64,
                    )
                )
                for statement in (
                    sa.update(AuditEvent)
                    .where(AuditEvent.id == identifier)
                    .values(actor="changed"),
                    sa.delete(AuditEvent).where(AuditEvent.id == identifier),
                ):
                    with pytest.raises(DBAPIError, match="append-only"):
                        async with connection.begin_nested():
                            await connection.execute(statement)
                assert (
                    await connection.scalar(
                        sa.select(AuditEvent.actor).where(AuditEvent.id == identifier)
                    )
                    == "postgres_test"
                )
            finally:
                await transaction.rollback()
        async with engine.connect() as connection:
            assert (
                await connection.scalar(sa.select(AuditEvent.id).where(AuditEvent.id == identifier))
                is None
            )
    finally:
        await engine.dispose()


async def hypertables(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    "SELECT hypertable_name FROM timescaledb_information.hypertables "
                    "WHERE hypertable_schema='public' AND hypertable_name IN ('candles','ticks')"
                )
            )
            assert set(rows.scalars()) == {"candles", "ticks"}
            retained = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE hypertable_schema='public' AND hypertable_name='ticks' "
                    "AND proc_name='policy_retention' AND scheduled "
                    "AND config->>'drop_after'='90 days'"
                )
            )
            assert retained == 1
    finally:
        await engine.dispose()


def fill(identifier, quantity, order_id="order"):
    return sa.insert(Trade).values(
        id=identifier,
        order_id=order_id,
        instrument_id="isolated_test",
        exchange_trade_id=identifier,
        transaction_type="BUY",
        quantity=quantity,
        price=100,
        executed_at=datetime.now(timezone.utc),
        mode="PAPER",
        execution_realism="SIMULATED",
    )


async def fill_limits(url):
    schema = "ats_fill_test_" + uuid4().hex
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": f'"{schema}", public'}}
    )
    try:
        async with engine.begin() as connection:
            enabled = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.trades'::regclass "
                    "AND tgname='trades_fill_sum' AND tgenabled='O' "
                    "AND tgfoid='public.ats_check_fill_sum()'::regprocedure"
                )
            )
            assert enabled == 1
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            for table in ("orders", "trades"):
                await connection.execute(
                    sa.text(f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)')
                )
            assert await connection.scalar(sa.text("SELECT current_schema()")) == schema
            await connection.execute(
                sa.text(
                    f'CREATE TRIGGER trades_fill_sum BEFORE INSERT OR UPDATE ON "{schema}".trades FOR EACH ROW EXECUTE FUNCTION public.ats_check_fill_sum()'
                )
            )
            enabled = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.orders'::regclass "
                    "AND tgname='orders_fill_limit' AND tgenabled='O' "
                    "AND tgfoid='public.ats_check_order_fill_limit()'::regprocedure"
                )
            )
            assert enabled == 1
            await connection.execute(
                sa.text(
                    f'CREATE TRIGGER orders_fill_limit BEFORE UPDATE OF quantity ON "{schema}".orders FOR EACH ROW WHEN (NEW.quantity IS DISTINCT FROM OLD.quantity) EXECUTE FUNCTION public.ats_check_order_fill_limit()'
                )
            )
            await connection.execute(
                sa.insert(Order).values(
                    id="order",
                    intent_id="test-intent",
                    broker_reference_id="test-reference",
                    instrument_id="isolated_test",
                    trading_symbol="ISOLATED",
                    exchange="NSE",
                    segment="CASH",
                    product="MIS",
                    order_type="LIMIT",
                    transaction_type="BUY",
                    quantity=100,
                    price=100,
                    mode="PAPER",
                    execution_realism="SIMULATED",
                )
            )
        async with engine.begin() as first:
            await first.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
            await first.execute(fill("first", 60))
            with pytest.raises(DBAPIError):
                async with first.begin_nested():
                    await first.execute(fill("overfill", 41))
            with pytest.raises(DBAPIError, match="exchange_trade_id"):
                async with first.begin_nested():
                    await first.execute(
                        fill("duplicate-exchange", 1).values(exchange_trade_id="first")
                    )
            with pytest.raises(DBAPIError, match="fills exceed"):
                async with first.begin_nested():
                    await first.execute(
                        sa.update(Trade).where(Trade.id == "first").values(quantity=101)
                    )
            with pytest.raises(DBAPIError, match="lower than recorded fills"):
                async with first.begin_nested():
                    await first.execute(
                        sa.update(Order).where(Order.id == "order").values(quantity=59)
                    )
            await first.execute(fill("remaining", 40))
            assert await first.scalar(sa.select(sa.func.sum(Trade.quantity))) == 100
        for isolation in ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"):
            async with engine.begin() as connection:
                await connection.execute(sa.delete(Trade))
            await concurrent_fills(engine, schema, isolation)
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def concurrent_fills(engine, schema, isolation):
    async with engine.connect() as first, engine.connect() as second:
        await second.execution_options(isolation_level=isolation)
        first_transaction = await first.begin()
        second_transaction = await second.begin()
        pending = None
        try:
            for connection in (first, second):
                await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
                await connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
            assert await second.scalar(sa.select(Order.quantity)) == 100
            await first.execute(fill("concurrent-first", 60))
            pending = asyncio.create_task(second.execute(fill("concurrent-second", 60)))
            await asyncio.sleep(0.15)
            assert not pending.done(), "Concurrent fill bypassed the order serialization lock"
            await first_transaction.commit()
            with pytest.raises(DBAPIError) as failure:
                await pending
            assert failure.value.orig.sqlstate in {"23514", "40001"}
            await second_transaction.rollback()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if first_transaction.is_active:
                await first_transaction.rollback()
            if second_transaction.is_active:
                await second_transaction.rollback()
    async with engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.sum(Trade.quantity))) == 60


async def downgrade_refused(url):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    migration = (
        ScriptDirectory.from_config(config).get_revision("0015_serialized_fill_limits").module
    )
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            before = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))

            def attempt(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    with pytest.raises(RuntimeError, match="Refusing to remove"):
                        migration.downgrade()

            await connection.run_sync(attempt)
            assert (
                await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == before
            )
            await connection.rollback()
    finally:
        await engine.dispose()
