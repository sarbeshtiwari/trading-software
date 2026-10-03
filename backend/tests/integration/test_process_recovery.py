"""Actual child-process termination/restart preserves durable PAPER economics."""

import asyncio
import os
import sys
from decimal import Decimal
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.brokers.paper.engine import FillConfig
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from tests.conftest import BACKEND_ROOT
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.process_database import process_database

__all__ = ["credentials"]


async def launch(worker, clock, action, cwd):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.paper_recovery_process", clock.now().isoformat(), action,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT), "TRADING_MODE": "PAPER",
             "BROKER_PROVIDER": "paper", "STARTING_CAPITAL": str(worker.settings.starting_capital),
             "PAPER_WORKER_ENABLED": "false", "DATABASE_URL": worker.settings.database_url},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def ready(process):
    for _attempt in range(100):
        line = await process.stdout.readline()
        if not line or line.strip() == b"PAPER_RECOVERED_WAITING_FOR_TEST_CRASH":
            return line
    raise AssertionError("subprocess never reported recovered state")


def safe_output(raw, database_url):
    output = raw.decode(errors="replace").replace(database_url, "<database URL>")
    password = make_url(database_url).password
    if password:
        output = output.replace(password, "<password>").replace(quote(password, safe=""), "<password>")
    return output


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("storage", ["sqlite", pytest.param(
    "postgres", marks=pytest.mark.skipif(
        not os.environ.get("ATS_TEST_POSTGRES_URL"), reason="set ATS_TEST_POSTGRES_URL"
    ),
)])
async def test_real_process_crash_then_restart_and_exit(
    db_engine, credentials, fake_clock, tmp_path, pending, storage, monkeypatch
):
    worker, _provider, api = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True,
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000 if pending else 0),
    )
    children = []
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            entry_id = order.id
            assert order.filled_quantity == (0 if pending else 111)
        await worker.stop()
        async with process_database(worker.settings, storage, monkeypatch):
            await crash_restart_and_assert(worker, fake_clock, tmp_path, children, entry_id)
    finally:
        for process in children:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await worker.stop()
        await api.aclose()


async def crash_restart_and_assert(worker, fake_clock, tmp_path, children, entry_id):
    try:
        first = await launch(worker, fake_clock, "hold", tmp_path)
        children.append(first)
        marker = await asyncio.wait_for(ready(first), timeout=30)
        if marker.strip() != b"PAPER_RECOVERED_WAITING_FOR_TEST_CRASH":
            _stdout, stderr = await asyncio.wait_for(first.communicate(), timeout=10)
            pytest.fail(safe_output(stderr, worker.settings.database_url))
        first.kill()
        await asyncio.wait_for(first.wait(), timeout=10)
        assert first.returncode != 0
        second = await launch(worker, fake_clock, "exit", tmp_path)
        children.append(second)
        stdout, stderr = await asyncio.wait_for(second.communicate(), timeout=45)
        assert second.returncode == 0, safe_output(stderr, worker.settings.database_url)
        assert b"PAPER_RECOVERED_AND_EXITED" in stdout.splitlines()
        async with db_session.session_scope() as session:
            orders = list(await session.scalars(sa.select(Order)))
            position = await session.scalar(sa.select(Position))
            journal = await session.scalar(sa.select(JournalEntry))
            assert len(orders) == 2
            assert sum(row.role == "ENTRY" and row.id == entry_id for row in orders) == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 2
            assert position.net_quantity == 0 and position.total_charges > 0
            assert journal.net_pnl == journal.gross_pnl - journal.charges
            assert journal.position_id == position.id
    finally:
        for process in children:
            if process.returncode is None:
                process.kill()
                await process.wait()
