"""Real PAPER services with isolated synthetic market and failure boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Order, Position
from app.modes import TradingMode
from app.trading.inputs import ReferenceInputStore
from app.trading.worker import _runtime
from tests.integration.test_option_evidence import credentials, setup_option_worker

__all__ = ["credentials"]


@pytest.mark.parametrize("defect", ["policy", "source"])
async def test_option_preflight_refuses_changed_or_forged_contract(
    db_engine, credentials, fake_clock, tmp_path, defect
):
    worker, provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.reference_runtime.cycle()
        async with db_session.session_scope() as session:
            proposal = await session.scalar(
                sa.select(Proposal).where(Proposal.strategy_id == "long-option-breakout")
            )
        assert proposal.status == "RISK_APPROVED"
        if defect == "policy":
            store = ReferenceInputStore(fake_clock)
            previous = await store.at("ins-test", provider.data_origin, fake_clock.now())
            fake_clock.advance(timedelta(seconds=1))
            await store.publish(
                previous.model_copy(
                    update={
                        "contract_source": previous.contract_source.model_copy(
                            update={
                                "minimum_days_to_expiry": 8,
                            }
                        )
                    }
                ),
                actor="test-owner",
                reason="Withdraw pending option permission",
            )
        else:
            async with db_session.session_scope() as session:
                row = await session.get(Proposal, proposal.id)
                row.context_snapshot = dict(
                    row.context_snapshot,
                    costs=dict(row.context_snapshot["costs"], source_id="forged-contract-source"),
                )
        with pytest.raises(SafetyError, match="OPTION_CONTRACT"):
            await worker.executor.submit(proposal.id)
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        assert worker.executor.broker.account.open_positions() == []
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("blocker", ["latch", "stale", "disabled"])
async def test_option_entry_gates_block_without_broker_orders(
    db_engine, credentials, fake_clock, tmp_path, blocker
):
    worker, provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    try:
        if blocker == "latch":
            await worker.executor.safety.trip_error(TradingMode.PAPER, DataOrigin.SYNTHETIC)
        elif blocker == "stale":
            provider.get_quote.return_value = replace(
                provider.get_quote.return_value,
                observed_at=fake_clock.now() - timedelta(seconds=100),
            )
        else:
            response = await client.post(
                "/api/v1/strategies/long-option-breakout/paper",
                json={
                    "version": "1",
                    "enabled": False,
                    "reason": "Owner disables option hypothesis",
                },
            )
            assert response.status_code == 200
        await worker.cycle()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        assert worker.executor.broker.account.open_positions() == []
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("action", ["missing_protection", "owner_flatten"])
async def test_option_emergency_closes_and_audits(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, action
):
    worker, _provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        if action == "missing_protection":
            async with db_session.session_scope() as session:
                position = await session.scalar(sa.select(Position))
                position.stop_loss_price = None
            with pytest.raises(SafetyError, match="PROTECTION_MISSING_OR_CHANGED"):
                await worker.executor.verify_protection()
        else:
            monkeypatch.setattr(_runtime, "worker", worker)
            response = await client.post(
                "/api/v1/emergency",
                json={
                    "action": "FLATTEN",
                    "reason": "Owner closes isolated option position",
                    "confirmation": "FLATTEN PAPER",
                },
            )
            assert response.status_code == 200, response.text
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 0
            assert action == "owner_flatten" or (
                await session.scalar(
                    sa.select(AuditEvent.id).where(AuditEvent.event_type == "PROTECTION_FAILURE")
                )
                is not None
            )
        assert worker.executor.broker.account.open_positions() == []
    finally:
        await worker.stop()
        await client.aclose()


async def test_option_policy_revoked_after_preflight_never_reaches_broker(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    original = worker.executor._dispatch

    async def revoke_before_dispatch(identifier, **arguments):
        store = ReferenceInputStore(fake_clock)
        previous = await store.at("ins-test", provider.data_origin, fake_clock.now())
        fake_clock.advance(timedelta(seconds=1))
        await store.publish(previous, actor="test-owner", reason="Replace reviewed option policy")
        await original(identifier, **arguments)

    monkeypatch.setattr(worker.executor, "_dispatch", revoke_before_dispatch)
    try:
        await worker.cycle()
        async with db_session.session_scope() as session:
            orders = list((await session.scalars(sa.select(Order))).all())
            assert len(orders) == 1
            assert orders[0].broker_order_id is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
        assert await worker.executor.broker.list_orders() == []
    finally:
        await worker.stop()
        await client.aclose()


async def test_option_broker_failure_never_invents_fill_or_retries_submission(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    failure = AsyncMock(side_effect=RuntimeError("isolated PAPER broker failure"))
    monkeypatch.setattr(worker.executor.broker, "place_order", failure)
    try:
        await worker.cycle()
        await worker.cycle()
        assert failure.await_count == 1
        async with db_session.session_scope() as session:
            orders = list((await session.scalars(sa.select(Order))).all())
            assert len(orders) == 1
            assert orders[0].status.value == "UNKNOWN"
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
        assert worker.executor.broker.account.cash == Decimal(100000)
        assert worker.executor.broker.account.open_positions() == []
    finally:
        await worker.stop()
        await client.aclose()
