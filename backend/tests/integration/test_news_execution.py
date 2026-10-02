"""News-dependent approvals traverse real PAPER execution and recheck evidence at dispatch."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.enums import ExitReason, OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.marketdata.models import DepthLevel
from app.strategies.registry import StrategyRegistry
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.integration.test_research_sentiment import policy_request, seed
from tests.quant_fixture import quant_decision
from tests.unit.test_costs import schedule
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy

__all__ = ["credentials"]


async def prepared(credentials, fake_clock, monkeypatch, *, maximum_age=180, costed=False):
    engine, plain_proposal, market, client, context = await setup_execution(
        credentials, fake_clock, risk_cost=Decimal("0.5") if costed else Decimal(0)
    )
    try:
        if costed:
            response = await client.post(
                "/api/v1/strategies/reference/fees",
                json={
                    "schedule": schedule().model_dump(mode="json"),
                    "reason": "Owner publishes isolated synthetic execution tariff",
                },
            )
            assert response.status_code == 200, response.text
        spec = context.strategy.model_copy(
            update={
                "version": "news-execution",
                "required_inputs": ("candles", "news", "sentiment"),
            }
        )
        await StrategyRegistry().register(FixtureStrategy(spec), enabled_paper=True)
        context = context.model_copy(update={"strategy": spec})
        source_id = await seed(client, monkeypatch, "primary", "POSITIVE")
        request = policy_request()
        request["policy"]["aggregation"]["max_age_seconds"] = maximum_age
        assert (await client.put("/api/v1/news/sentiment-policy", json=request)).status_code == 200
        result = await quant_decision(
            DecisionPipeline(validator()),
            payload(evidence=[{"kind": "NEWS", "source_id": source_id}]),
            context,
        )
        assert result.code == "RISK_APPROVED"
        return engine, result.proposal_id, plain_proposal, market, client
    except BaseException:
        await client.aclose()
        raise


async def disable_source(client):
    current = (await client.get("/api/v1/news/sources/primary")).json()
    current["configuration"]["is_enabled"] = False
    response = await client.put(
        "/api/v1/news/sources/primary",
        json={
            "configuration": current["configuration"],
            "expected_event_id": current["event_id"],
            "reason": "Owner disables isolated source after proposal approval",
        },
    )
    assert response.status_code == 200, response.text


async def test_sentiment_approval_fills_recovers_exits_and_reaches_journal_api(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _plain, market, client = await prepared(
        credentials, fake_clock, monkeypatch, costed=True
    )
    try:
        market["asks"] = (DepthLevel(Decimal(100), 100), DepthLevel(Decimal(100), 10000))
        order_id = await engine.submit(proposal)
        assert await engine.submit(proposal) == order_id
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 200 and position.stop_loss_price == 98
            position_id = position.id
        await engine.verify_protection()
        await db_session.dispose_engine()
        db_session.init_engine()
        restarted = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restarted.recover()
        await restarted.verify_protection()
        await disable_source(client)
        market.pop("asks")
        market.update(bid=Decimal("104"), ask=Decimal("104.05"))
        await restarted.monitor_once()
        await restarted.exit(position_id, ExitReason.TARGET)
        async with db_session.session_scope() as session:
            position = await session.get(Position, position_id)
            journal = await session.scalar(sa.select(JournalEntry))
            assert position.net_quantity == 0 and position.realised_pnl == Decimal("800")
            assert journal.entry_order_id == order_id and journal.gross_pnl == Decimal("800")
            assert journal.net_pnl == Decimal("745.46") and journal.charges == Decimal("54.54")
        view = (await client.get("/api/v1/workspace")).json()
        assert len(view["orders"]) == 2 and len(view["fills"]) == 3
        assert Decimal(view["journal"][0]["net_pnl"]) == Decimal("745.46")
        assert view["positions"][0]["net_quantity"] == 0
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", ["source", "policy", "stale"])
async def test_news_changes_after_approval_block_broker_without_blocking_plain_strategy(
    db_engine, credentials, fake_clock, monkeypatch, change
):
    engine, proposal, plain, market, client = await prepared(
        credentials,
        fake_clock,
        monkeypatch,
        maximum_age=1 if change == "stale" else 180,
    )
    try:
        if change == "source":
            await disable_source(client)
        elif change == "policy":
            current = (await client.get("/api/v1/news/sentiment-policy")).json()
            request = policy_request(enabled=False, expected=current["event_id"])
            assert (
                await client.put("/api/v1/news/sentiment-policy", json=request)
            ).status_code == 200
        else:
            fake_clock.advance(timedelta(seconds=2))
            market["observed"] = fake_clock.now()
        with pytest.raises(SafetyError, match="NEWS_"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        await engine.submit(plain)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()


async def test_source_disable_between_preflight_and_dispatch_prevents_broker_call(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _plain, _market, client = await prepared(credentials, fake_clock, monkeypatch)
    try:
        original = engine._dispatch_entry_policy

        async def late_disable(identifier, market):
            await original(identifier, market)
            await disable_source(client)

        monkeypatch.setattr(engine, "_dispatch_entry_policy", late_disable)
        with pytest.raises(SafetyError, match="NEWS_EXECUTION"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.status == OrderStatus.CREATED
            order_id = order.id
        assert await engine.submit(proposal) == order_id
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()
