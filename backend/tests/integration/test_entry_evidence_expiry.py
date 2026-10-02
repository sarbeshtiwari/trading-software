"""Fresh proposals cannot renew old source evidence between decision and dispatch."""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.analysis.regime.classifier import classify
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_regime import inputs, observation, policy

__all__ = ["credentials"]


@pytest.mark.parametrize(
    "stage,source",
    [("pipeline", "regime"), ("submit", "regime"), ("dispatch", "regime"), ("submit", "costs")],
)
async def test_expired_source_blocks_actual_entry_path(
    db_engine, credentials, fake_clock, monkeypatch, stage, source
):
    engine, _, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        now = fake_clock.now()
        if source == "regime":
            decision = classify(
                inputs().model_copy(
                    update={"breadth": observation("0.6", at=now - timedelta(seconds=59))}
                ),
                policy(),
            )
            async with db_session.session_scope() as session:
                row = await session.get(RegimeHistory, "reg-test")
                row.decision = decision.model_dump(mode="json")
        else:
            context = context.model_copy(
                update={
                    "costs": context.costs.model_copy(
                        update={
                            "observed_at": now
                            - timedelta(
                                seconds=min(
                                    context.limits.max_market_age_seconds,
                                    context.limits.max_portfolio_age_seconds,
                                )
                                - 1
                            )
                        }
                    )
                }
            )
        if stage == "pipeline":
            fake_clock.advance_seconds(2)
            context = context.model_copy(
                update={
                    "market": context.market.model_copy(update={"as_of": fake_clock.now()}),
                    "prices": context.prices.model_copy(update={"as_of": fake_clock.now()}),
                }
            )
        decision = await quant_decision(DecisionPipeline(validator()), payload(), context)
        if stage == "pipeline":
            assert decision.code == "REGIME_NOT_PERMITTED"
        else:
            assert decision.code == "RISK_APPROVED"
            if stage == "dispatch":
                original = engine._dispatch

                async def delayed(*args, **kwargs):
                    fake_clock.advance_seconds(2)
                    market["observed"] = fake_clock.now()
                    return await original(*args, **kwargs)

                monkeypatch.setattr(engine, "_dispatch", delayed)
            else:
                fake_clock.advance_seconds(32 if source == "costs" else 2)
                market["observed"] = fake_clock.now()
            with pytest.raises(
                SafetyError,
                match="STALE_REGIME_EVIDENCE" if source == "regime" else "STALE_CONTRACT_EVIDENCE",
            ):
                await engine.submit(decision.proposal_id)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            orders = list((await session.scalars(sa.select(Order))).all())
            assert len(orders) == int(stage == "dispatch")
            assert all(
                order.status.value == "CREATED" and order.filled_quantity == 0 for order in orders
            )
    finally:
        await client.aclose()
