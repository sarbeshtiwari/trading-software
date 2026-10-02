"""Persisted product tampering must not escape the approved strategy contract."""

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.enums import OrderStatus, Product
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.config import StrategyRegistration
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.strategies.registry import StrategyRegistry
from app.strategies.signal import Signal
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_pipeline import signal_data
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy

__all__ = ["credentials"]


@pytest.mark.parametrize("changed", ["proposal", "context", "registration"])
async def test_preflight_rejects_changed_product_contract(
    db_engine, credentials, fake_clock, changed
):
    engine, proposal_id, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, proposal_id)
            if changed == "proposal":
                proposal.product = Product.CNC
            elif changed == "context":
                context = proposal.context_snapshot
                proposal.context_snapshot = context | {
                    "strategy": context["strategy"] | {"product": "CNC"}
                }
            else:
                registration = await session.scalar(sa.select(StrategyRegistration))
                registration.parameters = registration.parameters | {"product": "CNC"}
        with pytest.raises(SafetyError, match="STRATEGY_PRODUCT_OR_CONTRACT_MISMATCH"):
            await engine.submit(proposal_id)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
    finally:
        await client.aclose()


async def test_dispatch_rechecks_order_product_after_preflight(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal_id, _market, client, _context = await setup_execution(credentials, fake_clock)
    original = engine._dispatch

    async def tamper(identifier, **kwargs):
        async with db_session.session_scope() as session:
            (await session.get(Order, identifier)).product = Product.CNC
        return await original(identifier, **kwargs)

    monkeypatch.setattr(engine, "_dispatch", tamper)
    try:
        with pytest.raises(SafetyError, match="ORDER_PRODUCT_CONTRACT_MISMATCH"):
            await engine.submit(proposal_id)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            row = await session.scalar(sa.select(Order))
            assert row.status == OrderStatus.CREATED and row.submitted_at is None
    finally:
        await client.aclose()


async def test_declared_products_and_owner_positional_policy(db_engine, credentials, fake_clock):
    engine, _unused, _market, client, context = await setup_execution(credentials, fake_clock)
    pipeline = DecisionPipeline(validator())
    try:
        mismatch = await pipeline.process(Signal(**(signal_data() | {"product": "CNC"})), context)
        assert mismatch.code == "SIGNAL_CONTEXT_MISMATCH"
        specification = context.strategy.model_copy(update={"version": "2", "product": Product.CNC})
        await StrategyRegistry().register(FixtureStrategy(specification), enabled_paper=True)
        positional = context.model_copy(update={"strategy": specification})
        blocked = await quant_decision(pipeline, payload(), positional)
        assert blocked.code == "POSITIONAL_TRADING_DISABLED"
        listed = (await client.get("/api/v1/journal?kind=REJECTION")).json()["entries"]
        assert {item["rejection_code"] for item in listed} == {
            "SIGNAL_CONTEXT_MISMATCH",
            "POSITIONAL_TRADING_DISABLED",
        }
        credentials[0].allow_positional = True
        approved = await quant_decision(pipeline, payload(), positional)
        assert approved.approved_quantity == 250
        credentials[0].allow_positional = False
        with pytest.raises(SafetyError, match="POSITIONAL_TRADING_DISABLED"):
            await engine.submit(approved.proposal_id)
        assert await engine.broker.list_orders() == []
        credentials[0].allow_positional = True
        order_id = await engine.submit(approved.proposal_id)
        async with db_session.session_scope() as session:
            order = await session.get(Order, order_id)
            assert order.product == Product.CNC and order.status == OrderStatus.EXECUTED
        invalid = context.strategy.model_copy(update={"version": "3", "product": Product.NRML})
        await StrategyRegistry().register(FixtureStrategy(invalid), enabled_paper=True)
        outcome = await quant_decision(
            pipeline, payload(), context.model_copy(update={"strategy": invalid})
        )
        assert outcome.code == "PRODUCT_SEGMENT_MISMATCH"
    finally:
        await client.aclose()
