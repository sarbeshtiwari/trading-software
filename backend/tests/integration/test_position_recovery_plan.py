"""Recovery mathematics uses immutable real-service fixture fills, never synthetic entries."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal, RiskDecision
from app.db.models.trading import Order, Position, Trade
from app.execution.position_recovery import build
from tests.integration.test_orphan_review import adopted_fixture
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_reference_exits import quote_at
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


async def reviewed_orphan(api):
    item = (await api.get("/api/v1/reconciliation/orphans")).json()["items"][0]
    response = await api.post(f"/api/v1/reconciliation/orphans/{item['position_id']}/acknowledge", json={
        "reason": "Reviewed orphan before evidence reconstruction", "expected_head": item["head_hash"],
        "confirmation": "ACKNOWLEDGE PAPER ORPHAN",
    })
    assert response.status_code == 200
    return item["position_id"]


async def test_recovery_plan_replays_verified_fills_without_mutation(db_engine, credentials, fake_clock):
    engine, api = await adopted_fixture(credentials, fake_clock)
    try:
        item = (await api.get("/api/v1/reconciliation/orphans")).json()["items"][0]
        endpoint = f"/api/v1/reconciliation/orphans/{item['position_id']}/recovery-plan"
        assert (await api.get(endpoint)).status_code == 409
        identifier = await reviewed_orphan(api)
        response = await api.get(endpoint)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["quantity"] == 250 and Decimal(plan["average_price"]) == 100
        assert Decimal(plan["gross_realised_pnl"]) == 0
        assert Decimal(plan["recorded_charges"]) == 0 and not plan["charges_complete"]
        assert "EXPLICIT_RESTORATION_REQUIRED" in plan["blockers"]
        assert Decimal(plan["historical_protection"]["stop"]) == 98
        assert not plan["historical_protection"]["active_protection_verified"]
        assert (await api.get(endpoint)).json()["plan_hash"] == plan["plan_hash"]
        async with db_session.session_scope() as session:
            trade = await session.scalar(sa.select(Trade))
            assert plan["source_fill_ids"] == [trade.id]
            assert plan["original_position_id"] == trade.position_id
            assert await session.get(Position, trade.position_id) is None
            assert (await session.get(Position, identifier)).realised_pnl is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await api.aclose()


@pytest.mark.parametrize("corrupt_history", [False, True])
async def test_recovery_preserves_real_reference_trailing_history(
    db_engine, credentials, fake_clock, tmp_path, corrupt_history
):
    worker, provider, api = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        quote_at(provider, fake_clock, "106")
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.trailing_stop_price == 102
            await session.delete(position)
        with pytest.raises(SafetyError, match="ORPHAN_POSITION"):
            await worker.executor.recover()
        identifier = await reviewed_orphan(api)
        if corrupt_history:
            async with db_session.session_scope() as session:
                record = await session.scalar(sa.select(AuditEvent).where(
                    AuditEvent.event_type == "REFERENCE_EXIT_STATE"
                ).order_by(AuditEvent.sequence.desc()))
                record.result = {**record.result, "trailing_stop": "96"}
            with pytest.raises(SafetyError, match="RECOVERY_PROTECTION_HISTORY_INVALID"):
                await build(identifier, fake_clock.utcnow(), "owner")
            return
        plan = await build(identifier, fake_clock.utcnow(), "owner")
        assert plan.historical_protection.trailing_stop == 102
        assert plan.historical_protection.stop == 96
        assert plan.historical_protection.position_audit_head
        assert not plan.historical_protection.active_protection_verified
    finally:
        await worker.stop()
        await api.aclose()


@pytest.mark.parametrize("fault", ["price", "fifo", "missing", "future", "order", "stop", "risk"])
async def test_recovery_plan_refuses_unproven_history(db_engine, credentials, fake_clock, fault):
    _engine, api = await adopted_fixture(credentials, fake_clock)
    try:
        identifier = await reviewed_orphan(api)
        async with db_session.session_scope() as session:
            trade = await session.scalar(sa.select(Trade))
            if fault == "price":
                trade.price += 1
            elif fault == "fifo":
                trade.cost_breakdown = {**trade.cost_breakdown, "fifo": {"sequence": 2}}
            elif fault == "missing":
                await session.delete(trade)
            elif fault == "future":
                trade.executed_at = fake_clock.utcnow() + timedelta(seconds=1)
            elif fault == "order":
                (await session.get(Order, trade.order_id)).filled_quantity -= 1
            elif fault == "stop":
                order = await session.get(Order, trade.order_id)
                (await session.get(Proposal, order.proposal_id)).stop_loss -= 1
            else:
                order = await session.get(Order, trade.order_id)
                (await session.get(RiskDecision, order.risk_decision_id)).approved = False
        response = await api.get(f"/api/v1/reconciliation/orphans/{identifier}/recovery-plan")
        assert response.status_code == 409, response.text
        assert "RECOVERY_" in response.text or "APPROVAL_" in response.text
    finally:
        await api.aclose()


async def test_partial_exit_recovery_plan_preserves_fifo_profit(db_engine, credentials, fake_clock):
    engine, proposal, market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            original = await session.scalar(sa.select(Position))
            original_id = original.id
        market.update(quantity=100, bid=Decimal("104"), ask=Decimal("104.05"))
        exit_id = await engine.exit(original_id, ExitReason.MANUAL)
        await engine.cancel(exit_id)
        async with db_session.session_scope() as session:
            position = await session.get(Position, original_id)
            assert position.net_quantity == 150 and position.realised_pnl == 400
            await session.delete(position)
        with pytest.raises(SafetyError, match="ORPHAN_POSITION"):
            await engine.recover()
        identifier = await reviewed_orphan(api)
        response = await api.get(f"/api/v1/reconciliation/orphans/{identifier}/recovery-plan")
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["quantity"] == 150 and Decimal(plan["average_price"]) == 100
        assert Decimal(plan["gross_realised_pnl"]) == 400
        assert len(plan["source_fill_ids"]) == 2
        assert plan["lots"][0]["quantity"] == 150
        assert len(await engine.broker.list_orders()) == 2
        restored = await api.post(f"/api/v1/reconciliation/orphans/{identifier}/restore", json={
            "reason": "Reviewed partial exit and remaining FIFO accounting",
            "confirmation": "RESTORE PAPER POSITION", "expected_head": plan["plan_hash"],
        })
        assert restored.status_code == 200, restored.text
        async with db_session.session_scope() as session:
            position = await session.get(Position, original_id)
            assert position.net_quantity == 150 and position.realised_pnl == 400
            assert position.bought_quantity == 250 and position.sold_quantity == 100
        assert len(await engine.broker.list_orders()) == 2
    finally:
        await api.aclose()
