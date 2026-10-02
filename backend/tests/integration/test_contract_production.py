"""Actual PAPER contract observations; all market/tariff inputs are isolated fixtures."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.analysis.regime.history import RegimeStore
from app.analysis.regime.inputs import RegimeInputs
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order
from app.trading.contracts import CashContractSource
from app.trading.inputs import ReferenceInputStore
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_regime import inputs as regime_inputs
from tests.unit.test_regime import policy

__all__ = ["credentials"]


async def publish_policy(worker, provider, clock, *, seconds=600, reserve="0.5"):
    store = ReferenceInputStore(clock)
    previous = await store.at("ins-test", provider.data_origin, clock.now())
    known = clock.now()
    source = CashContractSource(
        instrument_id="ins-test",
        data_origin=provider.data_origin,
        source="isolated owner policy",
        known_at=known,
        valid_from=known,
        valid_until=known + timedelta(seconds=seconds),
        risk_cost_reserve_per_unit=Decimal(reserve),
    )
    clock.advance_seconds(1)
    publication = await store.publish(
        previous.model_copy(update={"costs": None, "contract_source": source}),
        actor="fixture-owner",
    )
    return source, publication


@pytest.mark.parametrize(
    "reserve,quantity,next_quantity,charges", [("0.5", 111, 94, "31.44"), ("0", 116, 97, "32.86")]
)
async def test_two_worker_trades_derive_new_costs_without_renewing_policy(
    db_engine, credentials, fake_clock, tmp_path, reserve, quantity, next_quantity, charges
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        source, publication = await publish_policy(worker, provider, fake_clock, reserve=reserve)
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert worker.reference_runtime.detail == "RISK_APPROVED", worker.reference_runtime.detail
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        fake_clock.advance_seconds(120)
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(110),
            observed_at=fake_clock.now(),
            bids=(replace(quote.bids[0], price=Decimal("109.95")),),
            asks=(replace(quote.asks[0], price=Decimal(110)),),
        )
        bars = provider.get_candles.return_value
        provider.get_candles.return_value = (
            *bars[2:],
            replace(
                bars[-1],
                ts=bars[-1].ts + timedelta(minutes=1),
                open=Decimal(100),
                low=Decimal(100),
                high=Decimal(108),
                close=Decimal(108),
            ),
            replace(
                bars[-1],
                ts=bars[-1].ts + timedelta(minutes=2),
                open=Decimal(108),
                low=Decimal(108),
                high=Decimal(110),
                close=Decimal(110),
            ),
        )
        async with db_session.session_scope() as session:
            old = await session.get(RegimeHistory, "reg-test")
            calendar = old.decision["inputs"]["calendar"]
        fresh = regime_inputs(at=fake_clock.now()).model_dump(mode="json")
        fresh["calendar"] = calendar
        await RegimeStore(fake_clock).record(RegimeInputs.model_validate(fresh), policy())
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.event_type == "PAPER_CONTRACT_OBSERVATION")
                        .order_by(AuditEvent.occurred_at)
                    )
                ).all()
            )
            orders = list((await session.scalars(sa.select(Order))).all())
            journal = await session.scalar(sa.select(JournalEntry))
        assert len(records) == 2
        assert [Decimal(record.result["exposure_per_unit"]) for record in records] == [100, 110]
        assert [Decimal(record.result["margin_per_unit"]) for record in records] == [20, 22]
        assert all(
            record.data_used["reference_publication_id"] == publication for record in records
        )
        assert all(
            record.data_used["policy"] == source.model_dump(mode="json") for record in records
        )
        assert len(orders) == 3
        assert [order.quantity for order in orders if order.role == "ENTRY"] == [
            quantity,
            next_quantity,
        ]
        assert journal.gross_pnl == quantity * 8
        assert journal.charges == Decimal(charges)
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("missing", ["tariff", "expired_policy"])
async def test_missing_effective_inputs_stand_down_without_order(
    db_engine, credentials, fake_clock, tmp_path, missing
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=False)
    try:
        await publish_policy(
            worker, provider, fake_clock, seconds=1 if missing == "expired_policy" else 600
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert worker.reference_runtime.detail == (
            "CONTRACT_POLICY_OR_RESTRICTION_EXPIRED"
            if missing == "expired_policy"
            else "CONTRACT_DERIVATION_UNAVAILABLE"
        )
        assert await worker.executor.broker.list_orders() == []
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("stage", ["pipeline", "submit", "dispatch"])
async def test_policy_expiry_rechecked_before_entry(
    db_engine, credentials, fake_clock, monkeypatch, stage
):
    executor, _, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        context = context.model_copy(
            update={
                "costs": context.costs.model_copy(
                    update={"valid_until": fake_clock.now() + timedelta(seconds=1)}
                )
            }
        )
        if stage == "pipeline":
            context = context.model_copy(
                update={"costs": context.costs.model_copy(update={"valid_until": fake_clock.now()})}
            )
            result = await quant_decision(DecisionPipeline(validator()), payload(), context)
            assert result.code == "EXPIRED_CONTRACT_POLICY"
        else:
            result = await quant_decision(DecisionPipeline(validator()), payload(), context)
            if stage == "dispatch":
                dispatch = executor._dispatch

                async def expired(*args, **kwargs):
                    fake_clock.advance_seconds(1)
                    return await dispatch(*args, **kwargs)

                monkeypatch.setattr(executor, "_dispatch", expired)
            else:
                fake_clock.advance_seconds(1)
            with pytest.raises(SafetyError, match="EXPIRED_CONTRACT_POLICY"):
                await executor.submit(result.proposal_id)
        assert await executor.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_authenticated_contract_policy_publication_rejects_ambiguous_or_future_inputs(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        previous = await ReferenceInputStore(fake_clock).at(
            "ins-test", provider.data_origin, fake_clock.now()
        )
        now = fake_clock.now()
        source = CashContractSource(
            instrument_id="ins-test",
            data_origin=provider.data_origin,
            source="isolated owner policy",
            known_at=now,
            valid_from=now,
            valid_until=now + timedelta(hours=1),
            risk_cost_reserve_per_unit=0,
        )
        body = {
            "inputs": previous.model_dump(mode="json")
            | {"contract_source": source.model_dump(mode="json")},
            "reason": "Owner configures derived PAPER costs",
        }
        assert (
            await client.post("/api/v1/strategies/reference/inputs", json=body)
        ).status_code == 422
        body["inputs"]["costs"] = None
        response = await client.post("/api/v1/strategies/reference/inputs", json=body)
        assert response.status_code == 200, response.text
        body["inputs"]["contract_source"]["known_at"] = (now + timedelta(seconds=1)).isoformat()
        body["inputs"]["contract_source"]["valid_from"] = (now + timedelta(seconds=1)).isoformat()
        assert (
            await client.post("/api/v1/strategies/reference/inputs", json=body)
        ).status_code == 422
        client.headers.pop("Authorization")
        assert (
            await client.post("/api/v1/strategies/reference/inputs", json=body)
        ).status_code == 401
    finally:
        await worker.stop()
        await client.aclose()
