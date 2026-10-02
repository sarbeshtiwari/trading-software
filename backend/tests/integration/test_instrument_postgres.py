import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import InvalidResponseError
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.instruments.loader import InstrumentLoader

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")

CSV = (
    "exchange,segment,trading_symbol,instrument_type,lot_size,tick_size,"
    "expiry_date,strike_price,buy_allowed,sell_allowed,is_reserved\n"
    "NSE,FNO,ISOLATED_TEST_CE,CE,50,0.05,2026-11-23,840,0,1,0\n"
)


async def test_postgres_instrument_import_is_atomic_and_restricted(monkeypatch):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    sa.text(
                        "CREATE TEMP TABLE instruments (LIKE public.instruments INCLUDING ALL) ON COMMIT DROP"
                    )
                )
                monkeypatch.setattr(
                    db_session,
                    "_sessionmaker",
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    ),
                )
                loader = InstrumentLoader()
                assert (await loader.load(CSV)).inserted == 1
                assert (await loader.load(CSV)).updated == 0
                with pytest.raises(InvalidResponseError, match="Duplicate"):
                    await loader.load(CSV + CSV.splitlines()[1] + "\n")
                async with db_session.session_scope() as session:
                    rows = (await session.scalars(sa.select(Instrument))).all()
                    assert len(rows) == 1
                    assert rows[0].is_restricted
                    assert rows[0].option_type.value == "CE"
                    assert rows[0].lot_size == 50
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
