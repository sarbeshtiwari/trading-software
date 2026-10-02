"""Explicit terminal exit retries preserve partial fills; unknown intents never retry."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position
from app.execution.paper import PaperExecution
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]


async def test_cancelled_partial_exit_can_be_explicitly_replaced(
    db_engine, credentials, fake_clock
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        market.update(bid=Decimal(104), ask=Decimal("104.05"), quantity=50)
        first = await engine.exit(position.id)
        await engine.cancel(first)
        assert await engine.exit(position.id) == first
        restored = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restored.recover()
        engine = restored
        fake_clock.advance(timedelta(seconds=1))
        market.update(
            bid=Decimal(105), ask=Decimal("105.05"), quantity=10000, observed=fake_clock.now()
        )
        replacement = await engine.exit(position.id, ExitReason.EMERGENCY, retry_terminal=True)
        assert replacement != first
        assert await engine.exit(position.id, retry_terminal=True) == replacement
        assert len(await engine.broker.list_orders()) == 3
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
        assert set(journal.exit_order_ids) == {first, replacement}
        assert journal.actual_exit == Decimal("104.80")
        assert journal.gross_pnl == 1200
    finally:
        await client.aclose()


async def test_unknown_exit_is_never_replaced(db_engine, credentials, fake_clock, monkeypatch):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        calls = []

        async def failing_boundary(request):
            calls.append(request)
            raise RuntimeError("isolated transport failure")

        monkeypatch.setattr(engine.broker, "place_order", failing_boundary)
        with pytest.raises(SafetyError, match="ORDER_UNKNOWN_NO_BLIND_RESUBMISSION"):
            await engine.exit(position.id)
        await engine.exit(position.id, retry_terminal=True)
        assert len(calls) == 1
    finally:
        await client.aclose()
