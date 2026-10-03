"""Restore real PAPER projections without inventing fills or authorizing entries."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.paper import PAPER_STATE_ID, PaperBrokerState
from app.db.models.system import SINGLETON_ID, SystemState
from app.db.models.trading import Order, Position, Trade
from app.execution import orphan_restore
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate
from app.trading.worker import WorkerLock
from tests.integration.test_orphan_review import adopted_fixture
from tests.integration.test_paper_execution import credentials
from tests.integration.test_position_recovery_plan import reviewed_orphan

__all__ = ["credentials"]


async def prepare(api):
    identifier = await reviewed_orphan(api)
    plan = (await api.get(f"/api/v1/reconciliation/orphans/{identifier}/recovery-plan")).json()
    return identifier, plan, {
        "reason": "Owner reviewed original fills and protection history",
        "confirmation": "RESTORE PAPER POSITION", "expected_head": plan["plan_hash"],
    }


async def test_restore_restart_and_actual_exit_preserve_original_history(
    db_engine, credentials, fake_clock
):
    engine, api = await adopted_fixture(credentials, fake_clock)
    try:
        identifier, plan, body = await prepare(api)
        endpoint = f"/api/v1/reconciliation/orphans/{identifier}/restore"
        response = await api.post(endpoint, json=body)
        assert response.status_code == 200, response.text
        assert not response.json()["entries_authorized"]
        assert not response.json()["protection_verified"]
        assert (await api.post(endpoint, json=body)).json() == response.json()
        async with db_session.session_scope() as session:
            assert await session.get(Position, identifier) is None
            position = await session.get(Position, plan["original_position_id"])
            assert position.net_quantity == 250 and position.average_price == 100
            assert position.stop_loss_price == 98 and not position.is_protected
            assert position.unrealised_pnl is None and position.marked_at is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
            record = await session.scalar(sa.select(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_RESTORED"
            ))
            assert record.result["archived_orphan"]["adopted_from_broker"]
            assert record.result["archived_orphan"]["realised_pnl"] is None
            linked = await session.scalar(sa.select(AuditEvent).where(
                AuditEvent.event_type == "PAPER_POSITION_RESTORED"
            ))
            assert linked.position_id == plan["original_position_id"]
            assert linked.result["restoration_audit_hash"] == record.record_hash
            assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 1
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restarted.recover()
        assert restarted.ready and not get_trading_gate().new_entries_allowed
        await restarted.verify_protection()
        await restarted.exit(plan["original_position_id"], ExitReason.MANUAL)
        async with db_session.session_scope() as session:
            position = await session.get(Position, plan["original_position_id"])
            assert position.net_quantity == 0
            assert position.realised_pnl == Decimal("-12.50")
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.gross_pnl == position.realised_pnl
        workspace = (await api.get("/api/v1/workspace")).json()
        assert not workspace["new_entries_allowed"]
    finally:
        await api.aclose()


@pytest.mark.parametrize("fault", [
    "plan", "notification", "auth", "broker", "worker", "pending", "confirmation",
])
async def test_restore_refusal_or_failure_leaves_orphan_unchanged(
    db_engine, credentials, fake_clock, monkeypatch, fault
):
    engine, api = await adopted_fixture(credentials, fake_clock)
    lock = WorkerLock(engine.settings.log_dir / "paper-worker.lock")
    try:
        identifier, plan, body = await prepare(api)
        if fault == "plan":
            body["expected_head"] = "0" * 64
        elif fault == "confirmation":
            body["confirmation"] = "ACKNOWLEDGE PAPER ORPHAN"
        elif fault == "notification":
            async def fail(*args, **kwargs):
                raise RuntimeError("isolated persistence failure")
            monkeypatch.setattr(orphan_restore, "enqueue", fail)
        elif fault == "broker":
            async with db_session.session_scope() as session:
                row = await session.get(PaperBrokerState, PAPER_STATE_ID)
                position = {**row.account["positions"][0], "average_price": "101"}
                position["fifo_lots"] = [{**position["fifo_lots"][0], "price": "101"}]
                row.account = {**row.account, "positions": [position]}
        elif fault == "worker":
            lock.acquire()
        elif fault == "pending":
            async with db_session.session_scope() as session:
                row = await session.get(PaperBrokerState, PAPER_STATE_ID)
                row.orders = {key: {**value, "status": "PENDING"}
                              for key, value in row.orders.items()}
        else:
            api.headers.pop("Authorization")
        response = await api.post(f"/api/v1/reconciliation/orphans/{identifier}/restore", json=body)
        assert response.status_code == {
            "plan": 409, "notification": 503, "auth": 401, "broker": 409, "worker": 409,
            "pending": 409, "confirmation": 422,
        }[fault]
        async with db_session.session_scope() as session:
            assert await session.get(Position, identifier) is not None
            assert await session.get(Position, plan["original_position_id"]) is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_RESTORED"
            )) == 0
    finally:
        lock.release()
        await api.aclose()
