"""Actual isolated PostgreSQL crash/restart preserves the production PAPER lifecycle."""

import asyncio
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.brokers.paper.engine import FillConfig
from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.monitoring.gate import get_trading_gate
from tests.docker_database import isolated_server
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.process_database import process_database

__all__ = ["credentials"]
pytestmark = pytest.mark.skipif(
    os.environ.get("ATS_TEST_DOCKER_RESTART") != "1", reason="set ATS_TEST_DOCKER_RESTART=1"
)


@pytest.mark.parametrize("pending", [False, True])
async def test_server_crash_preserves_entry_and_costed_exit(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, pending
):
    worker, _provider, api = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True,
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000 if pending else 0),
    )
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with isolated_server() as server:
            monkeypatch.setenv("ATS_TEST_POSTGRES_URL", server.url.render_as_string(hide_password=False))
            async with process_database(worker.settings, "postgres", monkeypatch):
                async with db_session.session_scope() as session:
                    entry = await session.scalar(sa.select(Order))
                    assert entry.filled_quantity == (0 if pending else 111)
                await server.crash()
                try:
                    with pytest.raises((OSError, SQLAlchemyError, asyncio.TimeoutError)):
                        await worker.executor.monitor_once()
                    assert not worker.executor.ready and not get_trading_gate().new_entries_allowed
                    with pytest.raises(SafetyError, match="RECOVERY_REQUIRED"):
                        await worker.executor.submit(entry.proposal_id)
                finally:
                    await server.start()
                response = await api.post("/api/v1/emergency", json={
                    "action": "RECOVER_WORKER", "confirmation": "RECOVER PAPER WORKER",
                    "reason": "Owner reviewed isolated database server crash and restart",
                })
                assert response.status_code == 200, response.text
                assert response.json()["entries_blocked"]
                assert not get_trading_gate().new_entries_allowed
                assert await worker.executor.submit(entry.proposal_id) == entry.id
                fake_clock.advance_seconds(2)
                await worker.executor.monitor_once()
                async with db_session.session_scope() as session:
                    position = await session.scalar(sa.select(Position))
                    assert position.net_quantity == 111
                await worker.executor.verify_protection()
                await worker.executor.exit(position.id, ExitReason.MANUAL)
                fake_clock.advance_seconds(2)
                await worker.executor.monitor_once()
                async with db_session.session_scope() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 2
                    position = await session.get(Position, position.id)
                    journal = await session.scalar(sa.select(JournalEntry))
                    assert position.net_quantity == 0 and position.total_charges > 0
                    assert journal.position_id == position.id
                    assert journal.net_pnl == journal.gross_pnl - journal.charges
    finally:
        await worker.stop()
        await api.aclose()
