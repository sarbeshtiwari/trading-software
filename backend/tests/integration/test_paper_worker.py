"""The actual worker drives the persisted pipeline/OMS lifecycle on fixture time."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from apscheduler.events import EVENT_JOB_EXECUTED

from app.core.calendar import SpecialSession, TradingCalendar
from app.core.enums import Exchange, Segment
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.system import Heartbeat
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.marketdata.models import InstrumentRef
from app.monitoring.gate import get_trading_gate, reset_trading_gate
from app.trading.worker import PaperWorker, StoredQuoteSource, WorkerLock
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]


@pytest.mark.parametrize("squareoff", [False, True])
async def test_worker_dispatch_monitor_exit_and_exclusive_ownership(
    db_engine, credentials, fake_clock, tmp_path, squareoff
):
    engine, _, market, client, _ = await setup_execution(credentials, fake_clock)
    worker = PaperWorker(
        engine, calendar=TradingCalendar(complete_years=[2026]), lock_path=tmp_path / "worker.lock"
    )
    try:
        engine.settings.paper_cycle_seconds = 1
        scheduled_cycle = asyncio.Event()
        worker.scheduler.add_listener(lambda event: scheduled_cycle.set(), EVENT_JOB_EXECUTED)
        await worker.start(schedule=not squareoff)
        for hour, minute, phase in (
            (8, 0, "PRE_MARKET"),
            (9, 16, "MARKET_OPEN"),
            (14, 50, "NO_ENTRY_WINDOW"),
            (15, 10, "EXIT_WINDOW"),
            (15, 30, "EOD_RECONCILIATION"),
            (16, 0, "MARKET_CLOSE"),
        ):
            original = fake_clock.now()
            fake_clock.set_to(original.replace(hour=hour, minute=minute))
            assert worker.session_phase() == phase
            fake_clock.set_to(original)
        contender = WorkerLock(tmp_path / "worker.lock")
        with pytest.raises(SafetyError, match="ANOTHER"):
            contender.acquire()
        await worker.cycle()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
            assert (await session.scalar(sa.select(Position))).net_quantity == 250
        if squareoff:
            fake_clock.set_to(fake_clock.now().replace(hour=15, minute=10))
        else:
            market.update(bid=Decimal("104"), ask=Decimal("104.05"))
            fake_clock.advance(timedelta(seconds=5))
        market["observed"] = fake_clock.now()
        await worker.cycle()
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 0
            assert position.realised_pnl == Decimal("-12.50" if squareoff else "1000")
            assert (await session.get(Heartbeat, "paper-worker")).detail["phase"] == (
                "EXIT_WINDOW" if squareoff else "INTRADAY"
            )
        if not squareoff:
            await asyncio.wait_for(scheduled_cycle.wait(), timeout=5)
        await worker.stop()
        contender.acquire()
        contender.release()
    finally:
        await worker.stop()
        await client.aclose()


async def test_worker_error_survives_restart_without_silently_rearming(
    db_engine, credentials, fake_clock, tmp_path
):
    engine, _, market, client, _ = await setup_execution(credentials, fake_clock)
    worker = PaperWorker(
        engine, calendar=TradingCalendar(complete_years=[2026]), lock_path=tmp_path / "worker.lock"
    )
    restarted = None
    try:
        await worker.start(schedule=False)
        await worker.cycle()
        fake_clock.advance(timedelta(seconds=20))
        await worker.cycle()
        assert worker.failed
        await worker.stop()
        reset_trading_gate()
        get_trading_gate().clear("startup")
        market["observed"] = fake_clock.now()
        restored_engine = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        restarted = PaperWorker(
            restored_engine, calendar=worker.calendar, lock_path=worker.lock.path
        )
        await restarted.start(schedule=False)
        assert restarted.failed and "PAPER_WORKER_REVIEW_REQUIRED" in get_trading_gate().reason()
        await restarted.cycle()
        async with db_session.session_scope() as session:
            assert (await session.get(Heartbeat, "paper-worker")).detail["failed"]
    finally:
        await worker.stop()
        if restarted:
            await restarted.stop()
        await client.aclose()


@pytest.mark.parametrize("unknown_session", [False, True])
async def test_incomplete_calendar_blocks_entries_and_source_does_not_invent_quotes(
    db_engine, credentials, fake_clock, tmp_path, unknown_session
):
    engine, _, _, client, _ = await setup_execution(credentials, fake_clock)
    calendar = (
        TradingCalendar(
            complete_years=[2026],
            special_sessions=[
                SpecialSession(fake_clock.now().date(), "Isolated unknown", None, None)
            ],
        )
        if unknown_session
        else TradingCalendar()
    )
    worker = PaperWorker(engine, calendar=calendar, lock_path=tmp_path / "worker.lock")
    try:
        await worker.start(schedule=False)
        await worker.cycle()
        expected = "SPECIAL_SESSION_UNAVAILABLE" if unknown_session else "CALENDAR_UNAVAILABLE"
        assert worker.phase == expected
        assert await engine.broker.list_orders() == []
        assert (
            await StoredQuoteSource(fake_clock)(InstrumentRef("TEST", Exchange.NSE, Segment.CASH))
            is None
        )
    finally:
        await worker.stop()
        await client.aclose()
