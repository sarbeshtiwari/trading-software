"""FIFO uses actual PAPER fills, durable OMS state and restart recovery."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position, Trade
from app.execution.paper import PaperExecution
from app.marketdata.models import DepthLevel
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]


async def test_partial_exit_fifo_restart_and_journal(db_engine, credentials, fake_clock):
    engine, proposal, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        market["asks"] = (DepthLevel(Decimal(99), 100), DepthLevel(Decimal(100), 150))
        market["bid"] = Decimal("98.95")
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        market.pop("asks")
        market.update(bid=Decimal(104), ask=Decimal("104.05"), quantity=50)
        exit_id = await engine.exit(position.id, ExitReason.MANUAL)
        async with db_session.session_scope() as session:
            partial = await session.get(Position, position.id)
        assert partial.net_quantity == 200
        assert partial.average_price == Decimal("99.75")
        assert partial.realised_pnl == Decimal(250)
        assert (await engine._account(context)).realised_day_pnl == Decimal(250)
        assert engine.broker.account.realised_pnl == Decimal(250)
        restored = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restored.recover()
        await restored.sync(exit_id)
        assert restored.broker.account.realised_pnl == Decimal(250)
        fake_clock.advance(timedelta(seconds=1))
        market.update(
            bid=Decimal(105), ask=Decimal("105.05"), quantity=10000, observed=fake_clock.now()
        )
        await restored.monitor_once()
        async with db_session.session_scope() as session:
            closed = await session.get(Position, position.id)
            journal = await session.scalar(sa.select(JournalEntry))
            trades = list((await session.scalars(sa.select(Trade))).all())
        assert closed.net_quantity == 0
        assert closed.realised_pnl == Decimal(1300)
        assert journal.gross_pnl == Decimal(1300)
        assert journal.net_pnl is None
        assert restored.broker.account.cash == Decimal(101300)
        assert sorted(trade.cost_breakdown["fifo"]["sequence"] for trade in trades) == [1, 2, 3, 4]
        closed_lots = [
            match for trade in trades for match in trade.cost_breakdown["fifo"]["matches"]
        ]
        assert sorted(match["quantity"] for match in closed_lots) == [50, 50, 150]
        assert sum(Decimal(match["gross_pnl"]) for match in closed_lots) == 1300
        workspace = (await client.get("/api/v1/workspace")).json()
        assert Decimal(workspace["journal"][0]["gross_pnl"]) == 1300
        assert len(workspace["fills"]) == 4
        assert {row["order_id"] for row in workspace["fills"]} == {
            row["id"] for row in workspace["orders"]
        }
    finally:
        await client.aclose()


async def test_restart_rejects_cost_basis_discrepancy_despite_matching_quantity(
    db_engine, credentials, fake_clock
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            position.average_price += Decimal(1)
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        with pytest.raises(SafetyError, match="PAPER_RECONCILE_REQUIRED"):
            await restored.recover()
        assert not restored.ready
        assert len(await restored.broker.list_orders()) == 1
    finally:
        await client.aclose()
