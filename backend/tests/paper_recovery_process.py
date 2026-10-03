"""Isolated subprocess driver for real PAPER services; never a production entry point."""

import asyncio
import sys
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.core.calendar import TradingCalendar
from app.core.clock import FakeClock, set_clock
from app.core.data_origin import DataOrigin
from app.core.enums import ExitReason
from app.db import session as db_session
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.marketdata.stored_quotes import stored_quote
from app.modes import TradingMode
from app.trading.worker import PaperWorker


async def main():
    settings = get_settings()
    database = make_url(settings.database_url)
    isolated = database.drivername == "sqlite+aiosqlite" or (
        database.drivername == "postgresql+asyncpg"
        and (database.database or "").startswith("ats_recovery_test_")
    )
    if (settings.trading_mode != TradingMode.PAPER or settings.has_groww_credentials
            or not isolated):
        raise RuntimeError("isolated PAPER fixture database required")
    clock = FakeClock(datetime.fromisoformat(sys.argv[1]))
    set_clock(clock)

    async def source(instrument):
        return await stored_quote(instrument, as_of=clock.utcnow(), origin=DataOrigin.SYNTHETIC)

    executor = PaperExecution(source, settings=settings, clock=clock)
    worker = PaperWorker(executor, calendar=TradingCalendar(complete_years=[clock.today().year]))
    await worker.start(schedule=False)
    try:
        async with db_session.session_scope() as session:
            entry = await session.scalar(sa.select(Order).where(Order.role == "ENTRY"))
        if entry is None or await executor.submit(entry.proposal_id) != entry.id:
            raise RuntimeError("persisted entry identity not recovered")
        if sys.argv[2] == "hold":
            print("PAPER_RECOVERED_WAITING_FOR_TEST_CRASH", flush=True)
            await asyncio.Event().wait()
        else:
            clock.advance_seconds(2)
            await executor.monitor_once()
            async with db_session.session_scope() as session:
                position = await session.scalar(sa.select(Position).where(Position.net_quantity != 0))
            if position is None:
                raise RuntimeError("persisted entry did not produce a recoverable position")
            await executor.exit(position.id, ExitReason.MANUAL)
            clock.advance_seconds(2)
            await executor.monitor_once()
            print("PAPER_RECOVERED_AND_EXITED", flush=True)
    finally:
        await worker.stop()
        await db_session.dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
