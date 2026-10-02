"""Mutable execution state cannot be reused from a future point in time."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.system import PortfolioSnapshot
from app.db.models.trading import Position, Trade
from app.modes import TradingMode
from app.portfolio.cost_store import CostStore
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_costs import schedule
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


async def test_future_peak_does_not_contaminate_execution_risk(db_engine, credentials, fake_clock):
    engine, proposal_id, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            session.add(
                PortfolioSnapshot(
                    ts=fake_clock.utcnow() + timedelta(seconds=1),
                    mode=TradingMode.PAPER,
                    equity=Decimal("900000"),
                )
            )
        account = await engine.portfolio_state(context.strategy.id, context.market.data_origin)
        assert account.equity == account.peak_equity == 100000
        assert account.realised_day_pnl == 0
        await engine.submit(proposal_id)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("field", ["fill", "marked_at", "closed_at", "opened_at"])
async def test_future_account_state_blocks_an_independently_approved_new_order(
    db_engine, credentials, fake_clock, field
):
    engine, proposal_id, market, client, context = await setup_execution(
        credentials, fake_clock, risk_cost=Decimal("0.5")
    )
    await CostStore(fake_clock).publish(
        schedule(), actor="fixture-owner", reason="Synthetic fee evidence"
    )
    try:
        await engine.submit(proposal_id)
        async with db_session.session_scope() as session:
            position_id = await session.scalar(sa.select(Position.id))
        market.update(bid=Decimal(104), ask=Decimal("104.05"))
        await engine.exit(position_id)
        market.update(bid=Decimal("99.95"), ask=Decimal(100))
        account = await engine.portfolio_state(context.strategy.id, context.market.data_origin)
        candidate = await quant_decision(
            DecisionPipeline(validator()),
            payload(),
            context.model_copy(update={"cycle_id": "future-account", "portfolio": account}),
        )
        assert candidate.approved_quantity > 0
        async with db_session.session_scope() as session:
            future = fake_clock.utcnow() + timedelta(seconds=1)
            if field == "fill":
                trade = await session.scalar(sa.select(Trade).limit(1))
                trade.executed_at = future
            else:
                position = await session.get(Position, position_id)
                setattr(position, field, future)
        with pytest.raises(SafetyError, match="FUTURE_PAPER_ACCOUNT_STATE"):
            await engine.submit(candidate.proposal_id)
        assert len(await engine.broker.list_orders()) == 2
    finally:
        await client.aclose()
