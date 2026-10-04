"""Actual durable PAPER entry commitments are not free risk capacity."""

import os
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.brokers.paper.engine import FillConfig
from app.core.enums import TransactionType
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.db.models.trading import Order
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.process_database import process_database

__all__ = ["credentials"]


async def test_pending_entry_reserves_capacity_and_cancel_releases_it(
    db_engine, credentials, fake_clock
):
    engine, proposal, _market, client, context = await setup_execution(
        credentials, fake_clock, fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000)
    )
    try:
        await engine.submit(proposal)
        account = await engine.portfolio_state(context.strategy.id, context.market.data_origin)
        assert account.open_and_pending_positions == 1
        assert account.strategy_open_and_pending_positions == 1
        assert account.exposures[0].notional == Decimal(25000)
        assert account.reserved_risk == Decimal(500)
        assert context.costs.margin_per_unit == Decimal(100)
        assert account.available_margin == Decimal(75000)
        initial = (await client.get("/api/v1/workspace")).json()
        assert initial["account"] is None and initial["account_status"] == "UNAVAILABLE"
        await engine.monitor_once()
        state = (await client.get("/api/v1/workspace")).json()
        assert state["positions"] == [] and state["fills"] == []
        assert Decimal(state["account"]["available_margin"]) == Decimal(75000)
        assert Decimal(state["account"]["gross_exposure"]) == Decimal(25000)
        other = await engine.portfolio_state("another-strategy", context.market.data_origin)
        assert other.open_and_pending_positions == 1
        assert other.strategy_open_and_pending_positions == 0
        assert await PaperOrderHygiene(engine).run(all_pending=True)
        cleared = await engine.portfolio_state(context.strategy.id, context.market.data_origin)
        assert cleared.open_and_pending_positions == 0
        assert cleared.exposures == () and cleared.reserved_risk == 0
        assert cleared.available_margin == Decimal(100000)
    finally:
        await client.aclose()


@pytest.mark.parametrize("tamper", ["preflight", "price", "side", "future"])
async def test_pending_reservation_refuses_altered_preflight(
    db_engine, credentials, fake_clock, tamper
):
    engine, proposal, _market, client, context = await setup_execution(
        credentials, fake_clock, fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000)
    )
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            decision = await session.get(RiskDecision, order.risk_decision_id)
            if tamper == "preflight":
                decision.state_snapshot = decision.state_snapshot | {"proposal": {}}
            elif tamper == "price":
                order.price = Decimal(99)
            elif tamper == "side":
                order.transaction_type = TransactionType.SELL
            else:
                order.created_at = fake_clock.utcnow() + timedelta(seconds=1)
        with pytest.raises(SafetyError, match="PENDING_ENTRY_EVIDENCE_"):
            await engine.portfolio_state(context.strategy.id, context.market.data_origin)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()


async def test_partial_entry_and_restart_do_not_double_count_commitment(
    db_engine, credentials, fake_clock
):
    engine, proposal, market, client, context = await setup_execution(
        credentials, fake_clock, fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000)
    )
    try:
        await engine.submit(proposal)
        restored = PaperExecution(engine.quote, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        before = await restored.portfolio_state(context.strategy.id, context.market.data_origin)
        assert before.open_and_pending_positions == 1
        assert before.reserved_risk == 500 and before.available_margin == 75000
        market["quantity"] = 50
        fake_clock.advance_seconds(2)
        market["observed"] = fake_clock.now()
        await restored.monitor_once()
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.filled_quantity == 50 and order.quantity == 250
        partial = await restored.portfolio_state(context.strategy.id, context.market.data_origin)
        assert partial.open_and_pending_positions == partial.strategy_open_and_pending_positions == 1
        assert sum(item.notional for item in partial.exposures) == 25000
        assert partial.reserved_risk == 500
        assert partial.available_margin == restored.broker.account.available_margin - 20000
    finally:
        await client.aclose()


@pytest.mark.skipif(not os.environ.get("ATS_TEST_POSTGRES_URL"), reason="set ATS_TEST_POSTGRES_URL")
async def test_postgres_pending_recovery_and_workspace_reservation(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _market, client, context = await setup_execution(
        credentials, fake_clock, fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000)
    )
    try:
        await engine.submit(proposal)
        async with process_database(engine.settings, "postgres", monkeypatch):
            restored = PaperExecution(engine.quote, settings=engine.settings, clock=fake_clock)
            await restored.recover()
            account = await restored.portfolio_state(context.strategy.id, context.market.data_origin)
            assert account.open_and_pending_positions == 1
            assert account.reserved_risk == 500 and account.available_margin == 75000
            await restored.monitor_once()
            response = await client.get("/api/v1/workspace")
            assert response.status_code == 200
            state = response.json()
            assert state["positions"] == [] and state["fills"] == []
            assert len(state["orders"]) == 1
            assert Decimal(state["account"]["available_margin"]) == 75000
            assert Decimal(state["account"]["gross_exposure"]) == 25000
            assert await restored.submit(proposal) == state["orders"][0]["id"]
            assert len(await restored.broker.list_orders()) == 1
    finally:
        await client.aclose()
