"""Actual broker charges, OMS, risk account and journal consume one sourced tariff."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position, Trade
from app.execution.paper import PaperExecution
from app.marketdata.models import DepthLevel
from app.portfolio.cost_store import CostStore
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.unit.test_costs import schedule

__all__ = ["credentials"]


async def test_tariff_fill_charges_survive_restart_and_reach_journal_and_api(
    db_engine, credentials, fake_clock
):
    executor, proposal, market, client, context = await setup_execution(
        credentials, fake_clock, risk_cost=Decimal("0.5")
    )
    try:
        response = await client.post(
            "/api/v1/strategies/reference/fees",
            json={
                "schedule": schedule().model_dump(mode="json"),
                "reason": "Owner publishes isolated synthetic tariff",
            },
        )
        assert response.status_code == 200, response.text
        market["asks"] = (DepthLevel(Decimal(100), 100), DepthLevel(Decimal(100), 10000))
        order_id = await executor.submit(proposal)
        assert executor.broker.account.charges_paid == Decimal("24.96")
        account = await executor._account(context)
        assert account.realised_day_pnl == Decimal("-24.96")
        restored = PaperExecution(executor.source, settings=executor.settings, clock=fake_clock)
        await restored.recover()
        await restored.sync(order_id)
        assert restored.broker.account.charges_paid == Decimal("24.96")
        market.pop("asks")
        market.update(bid=Decimal(104), ask=Decimal("104.05"))
        await restored.monitor_once()
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            position = await session.scalar(sa.select(Position))
            trades = list((await session.scalars(sa.select(Trade))).all())
        assert journal.gross_pnl == 800
        assert journal.charges == Decimal("54.54")
        assert journal.net_pnl == Decimal("745.46")
        assert position.total_charges == journal.charges
        assert sum(trade.total_charges for trade in trades) == journal.charges
        assert restored.broker.account.cash == Decimal("100745.46")
        assert all(trade.cost_breakdown["status"] == "ESTIMATED" for trade in trades)
        response = await client.get("/api/v1/workspace")
        assert Decimal(response.json()["journal"][0]["net_pnl"]) == Decimal("745.46")
        assert Decimal(response.json()["account"]["charges"]) == Decimal("54.54")
    finally:
        await client.aclose()


async def test_published_fees_cannot_exceed_risk_budget_silently(
    db_engine, credentials, fake_clock
):
    executor, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await CostStore(fake_clock).publish(
            schedule(), actor="owner", reason="Synthetic tariff test"
        )
        with pytest.raises(SafetyError, match="PER_TRADE_RISK_EXCEEDED"):
            await executor.submit(proposal)
        assert await executor.broker.list_orders() == []
        async with db_session.session_scope() as session:
            decision = await session.scalar(
                sa.select(RiskDecision).where(RiskDecision.is_preflight.is_(True))
            )
        assert not decision.approved
        assert decision.risk_amount > 500
    finally:
        await client.aclose()
