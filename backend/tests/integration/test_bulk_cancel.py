"""Bulk intent is atomic; recovery never loses unprocessed order targets."""

from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.enums import OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order
from app.execution import bulk_cancel
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from app.trading.worker import PaperWorker
from tests.integration.test_owner_cancel import prepare, request_body
from tests.integration.test_paper_execution import credentials

__all__ = ["credentials"]


async def test_bulk_restart_recovers_unprocessed_intent_and_owner_result(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    body = request_body()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                bulk_cancel,
                "cancel_entry_locked",
                AsyncMock(side_effect=RuntimeError("test crash")),
            )
            with pytest.raises(RuntimeError, match="test crash"):
                await bulk_cancel.cancel_entries(
                    worker, actor="owner", request_id=body["request_id"], reason=body["reason"]
                )
        await worker.stop()
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        worker = PaperWorker(
            restored, calendar=worker.calendar, lock_path=tmp_path / "restored-bulk.lock"
        )
        await worker.start(schedule=False)
        assert await PaperOrderHygiene(restored).run()
        result = await bulk_cancel.cancel_entries(
            worker, actor="owner", request_id=body["request_id"], reason=body["reason"]
        )
        assert result["resolved"]
        assert result["outcomes"][0]["order_id"] == identifier
        assert result["outcomes"][0]["status"] == "CANCELLED"
        assert len(await restored.broker.list_orders()) == 1
        assert await AuditService().verify(result["audit_chain_id"], expected_count=2)
    finally:
        await worker.stop()
        await client.aclose()


async def test_bulk_api_cancels_real_partial_entry_and_replays(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    body = {**request_body(), "confirmation": "CANCEL PAPER ENTRIES"}
    try:
        response = await client.post("/api/v1/orders/cancel-entries", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["resolved"]
        assert result["excluded_exit_ids"] == []
        assert len(result["outcomes"]) == 1
        assert result["outcomes"][0]["order_id"] == identifier
        assert result["outcomes"][0]["filled_quantity"] == 100
        assert result["outcomes"][0]["status"] == "CANCELLED"
        replay = await client.post("/api/v1/orders/cancel-entries", json=body)
        assert replay.json() == result
        assert len(await engine.broker.list_orders()) == 1
        conflict = await client.post(
            "/api/v1/orders/cancel-entries",
            json={**body, "reason": "Changed cancellation request reason"},
        )
        assert conflict.status_code == 409
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("fail_before_commit", [False, True])
async def test_bulk_snapshot_durable_before_first_broker_call(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, fail_before_commit
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    async with db_session.session_scope() as session:
        original = await session.get(Order, identifier)
        values = {column.name: getattr(original, column.name) for column in Order.__table__.columns}
        values.update(
            id="isolated-untracked-entry",
            intent_id="isolated-entry-intent",
            broker_reference_id="isolated-bulk-reference",
            broker_order_id=None,
            filled_quantity=0,
            average_fill_price=None,
            position_id=None,
            status=OrderStatus.SUBMITTED,
        )
        session.add(Order(**values))
        values = {
            **values,
            "id": "isolated-protective-exit",
            "intent_id": "isolated-exit-intent",
            "broker_reference_id": "isolated-exit-reference",
            "role": "EXIT",
        }
        session.add(Order(**values))
    original_append = AuditService.append_in_session

    async def interrupted(self, session, identity, details, **kwargs):
        if fail_before_commit and identity.event_type == "ENTRY_CANCEL_INTENT":
            raise RuntimeError("test atomic audit failure")
        return await original_append(self, session, identity, details, **kwargs)

    cancel = AsyncMock(side_effect=RuntimeError("test process interruption"))
    body = request_body()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(AuditService, "append_in_session", interrupted)
            scoped.setattr(bulk_cancel, "cancel_entry_locked", cancel)
            with pytest.raises(RuntimeError, match="test"):
                await bulk_cancel.cancel_entries(
                    worker, actor="owner", request_id=body["request_id"], reason=body["reason"]
                )
        pending = await PaperOrderHygiene(engine).unfinished()
        if fail_before_commit:
            assert pending == {}
            cancel.assert_not_awaited()
            async with db_session.session_scope() as session:
                assert (
                    await session.scalar(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "BULK_ENTRY_CANCEL_INTENT"
                        )
                    )
                    is None
                )
        else:
            assert set(pending) == {identifier, "isolated-untracked-entry"}
            assert "isolated-protective-exit" not in pending
            assert not await PaperOrderHygiene(engine).run()
            async with db_session.session_scope() as session:
                assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
                assert (
                    await session.get(Order, "isolated-untracked-entry")
                ).status == OrderStatus.UNKNOWN
            assert "PAPER_ORDER_CANCELLATION_UNRESOLVED" in engine.gate.reason()
    finally:
        await worker.stop()
        await client.aclose()
