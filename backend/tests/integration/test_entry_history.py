"""Real durable PAPER entries and closes, then shared decisions and preflight vetoes."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order
from app.execution.paper import PaperExecution
from app.marketdata.models import DepthLevel
from app.portfolio.cost_store import CostStore
from app.risk.audit import RiskAudit
from app.risk.entry_history import load_entry_history
from app.risk.entry_policy import entry_rules
from app.strategies.registry import StrategyRegistry
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_costs import schedule
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy

__all__ = ["credentials"]


@pytest.mark.parametrize("loss", [False, True])
async def test_recorded_history_vetoes_preapproved_entry_after_restart(
    db_engine, credentials, fake_clock, loss
):
    credentials[0].max_trades_per_day = 10 if loss else 1
    engine, first, market, client, context = await setup_execution(
        credentials, fake_clock, risk_cost=Decimal("0.5")
    )
    await CostStore(fake_clock).publish(
        schedule(), actor="test-owner", reason="Isolated fee fixture"
    )
    smaller = context.strategy.model_copy(
        update={
            "version": "2",
            "risk": context.strategy.risk.model_copy(
                update={"requested_risk_fraction": Decimal("0.0025")}
            ),
        }
    )
    await StrategyRegistry().register(FixtureStrategy(smaller), enabled_paper=True)
    second_context = context.model_copy(update={"strategy": smaller})
    second = await quant_decision(DecisionPipeline(validator()), payload(), second_context)
    assert second.approved_quantity > 0
    try:
        market["asks"] = (DepthLevel(Decimal("99.95"), 1), DepthLevel(Decimal(100), 10000))
        first_order = await engine.submit(first)
        assert await engine.submit(first) == first_order
        await engine.monitor_once()
        observed = (await client.get("/api/v1/workspace")).json()
        assert Decimal(observed["positions"][0]["unrealised_pnl"]) == Decimal("0.05")
        market.pop("asks")
        market.update(
            bid=Decimal("97.95" if loss else "104"), ask=Decimal("98" if loss else "104.05")
        )
        await engine.monitor_once()
        async with db_session.session_scope() as session:
            journal = await session.scalar(
                sa.select(JournalEntry).where(JournalEntry.kind == "TRADE")
            )
            assert journal is not None and (journal.net_pnl < 0) == loss
            history = await load_entry_history(session, context.market, credentials[0])
            assert history.counted_order_ids == (first_order,)
        market.update(bid=Decimal("99.95"), ask=Decimal(100))
        restored = PaperExecution(engine.source, settings=credentials[0], clock=fake_clock)
        await restored.recover()
        code = "LOSS_COOLOFF_ACTIVE" if loss else "DAILY_TRADE_COUNT_LIMIT"
        with pytest.raises(SafetyError, match=code):
            await restored.submit(second.proposal_id)
        rejected = await quant_decision(DecisionPipeline(validator()), payload(), second_context)
        assert rejected.code == code and rejected.approved_quantity == 0
        async with db_session.session_scope() as session:
            decisions = list((await session.scalars(sa.select(RiskDecision))).all())
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
        for decision in decisions:
            assert (await RiskAudit().replay(decision.id)).approved == decision.approved
        workspace = (await client.get("/api/v1/workspace")).json()
        assert any(row["rejection_code"] == code for row in workspace["preflights"])
        assert any(
            row["entry_policy"]["counted_order_ids"] == [first_order]
            for row in workspace["preflights"]
        )
        journals = (await client.get("/api/v1/journal?kind=REJECTION")).json()["entries"]
        assert any(row["rejection_code"] == code for row in journals)
        async with db_engine.begin() as connection:
            await connection.execute(sa.delete(JournalEntry).where(JournalEntry.kind == "TRADE"))
        async with db_session.session_scope() as session:
            incomplete = await load_entry_history(session, context.market, credentials[0])
        assert incomplete.unavailable_reason == "CLOSED_POSITION_JOURNAL_UNAVAILABLE"
    finally:
        await client.aclose()


async def test_pending_unknown_order_reserves_quota(
    db_engine, credentials, fake_clock, monkeypatch
):
    credentials[0].max_trades_per_day = 1
    engine, proposal_id, _market, client, context = await setup_execution(credentials, fake_clock)

    async def unavailable(_request):
        raise TimeoutError("isolated broker boundary failure")

    monkeypatch.setattr(engine.broker, "place_order", unavailable)
    try:
        with pytest.raises(SafetyError):
            await engine.submit(proposal_id)
        async with db_session.session_scope() as session:
            history = await load_entry_history(session, context.market, credentials[0])
        assert len(history.counted_order_ids) == 1
        assert any(
            not rule.passed and rule.rejection_code == "DAILY_TRADE_COUNT_LIMIT"
            for rule in entry_rules(history)
        )
    finally:
        await client.aclose()


async def test_dispatch_rejects_changed_order_origin(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal_id, _market, client, context = await setup_execution(credentials, fake_clock)
    original = engine._dispatch

    async def corrupt_origin(identifier, **kwargs):
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            order.request_payload = order.request_payload | {"data_origin": "UNAVAILABLE"}
        return await original(identifier, **kwargs)

    monkeypatch.setattr(engine, "_dispatch", corrupt_origin)
    try:
        with pytest.raises(SafetyError, match="ENTRY_ORDER_CONTEXT_MISMATCH"):
            await engine.submit(proposal_id)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            history = await load_entry_history(session, context.market, credentials[0])
        assert history.unavailable_reason == "ORDER_ORIGIN_UNAVAILABLE"
    finally:
        await client.aclose()


@pytest.mark.parametrize("partial", [False, True])
async def test_cancelled_entry_counts_only_when_filled(db_engine, credentials, fake_clock, partial):
    engine, proposal_id, market, client, context = await setup_execution(credentials, fake_clock)
    market["asks"] = (
        (DepthLevel(Decimal(100), 1), DepthLevel(Decimal("100.05"), 10000))
        if partial
        else (DepthLevel(Decimal("100.05"), 10000),)
    )
    try:
        identifier = await engine.submit(proposal_id)
        await engine.cancel(identifier)
        async with db_session.session_scope() as session:
            history = await load_entry_history(session, context.market, credentials[0])
        assert history.unavailable_reason is None
        assert history.counted_order_ids == ((identifier,) if partial else ())
    finally:
        await client.aclose()


async def test_history_refuses_future_decision_time(db_engine, credentials, fake_clock):
    _engine, _proposal_id, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        future = context.market.model_copy(
            update={"as_of": context.market.as_of + timedelta(seconds=1)}
        )
        async with db_session.session_scope() as session:
            history = await load_entry_history(session, future, credentials[0])
        assert history.unavailable_reason == "HISTORY_TIME_IN_FUTURE"
        assert not entry_rules(history)[0].passed
    finally:
        await client.aclose()
