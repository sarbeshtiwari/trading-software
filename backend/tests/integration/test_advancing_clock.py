"""PAPER must work with a moving wall clock, not only frozen replay instants."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import FakeClock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from tests.integration.test_auth import credentials
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


async def test_reference_worker_trades_with_advancing_clock(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    original_now = FakeClock.now

    def advancing_now(clock):
        instant = original_now(clock)
        clock.advance(timedelta(microseconds=1))
        return instant

    monkeypatch.setattr(FakeClock, "now", advancing_now)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            evaluation = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "REFERENCE_EVALUATION")
            )
            assert evaluation.result["strategy_reason"] == "SIGNAL"
            assert evaluation.result["pipeline_code"] == "RISK_APPROVED"
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 111
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
        await worker.executor.verify_protection()
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert len(response.json()["orders"]) == 1
        assert provider.get_candles.await_count == 1
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.gross_pnl == Decimal(888)
            assert journal.charges == Decimal("31.44")
            assert journal.net_pnl == Decimal("856.56")
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 0
        assert await AuditService(fake_clock).verify(evaluation.chain_id)
        response = await client.get("/api/v1/workspace")
        assert len(response.json()["orders"]) == 2
        assert len(response.json()["journal"]) == 1
    finally:
        await worker.stop()
        await client.aclose()
