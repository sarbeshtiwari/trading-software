"""Authenticated controls exercise the real OMS and durable PAPER broker."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from app.api import orders as orders_api
from app.audit.service import AuditService
from app.core.calendar import TradingCalendar
from app.core.enums import OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from app.main import create_app
from app.modes import TradingMode
from app.trading.worker import PaperWorker
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


def request_body():
    return {
        "request_id": str(uuid4()),
        "reason": "Owner cancels isolated PAPER entry",
        "confirmation": "CANCEL PAPER ENTRY",
    }


async def prepare(credentials, fake_clock, tmp_path, monkeypatch):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "owner-cancel.lock",
    )
    await worker.start(schedule=False)
    monkeypatch.setattr(orders_api, "active_worker", lambda: worker)
    return engine, identifier, client, worker


async def test_owner_cancel_partial_entry_replay_and_api_state(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    try:
        body = request_body()
        response = await client.post(f"/api/v1/orders/{identifier}/cancel", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["filled_quantity"] == 100
        assert response.json()["error"] is None
        workspace = (await client.get("/api/v1/workspace")).json()
        visible = next(row for row in workspace["orders"] if row["id"] == identifier)
        assert visible["status"] == "CANCELLED"
        assert visible["role"] == "ENTRY"
        assert visible["filled_quantity"] == 100
        replay = await client.post(f"/api/v1/orders/{identifier}/cancel", json=body)
        assert replay.json() == response.json()
        conflict = await client.post(
            f"/api/v1/orders/{identifier}/cancel",
            json={**body, "reason": "Different owner request reason"},
        )
        assert conflict.status_code == 409
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 100
            assert position.stop_loss_price is not None
            journals = list(
                await session.scalars(
                    sa.select(JournalEntry).where(JournalEntry.kind == "ORDER_ACTION")
                )
            )
        assert len(journals) == 1
        records = await AuditService().chain(response.json()["audit_chain_id"])
        assert len(records) == 2
        assert records[0].actor == records[1].actor == "owner"
        assert await AuditService().verify(records[0].chain_id, expected_count=2)
        assert len(await engine.broker.list_orders()) == 1
        assert await PaperOrderHygiene(engine).run()
    finally:
        await worker.stop()
        await client.aclose()


async def test_owner_cancel_audit_failure_never_reaches_broker(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    cancel = AsyncMock(wraps=engine.cancel)
    monkeypatch.setattr(engine, "cancel", cancel)
    monkeypatch.setattr(
        PaperOrderHygiene, "record", AsyncMock(side_effect=RuntimeError("test audit outage"))
    )
    try:
        with pytest.raises(RuntimeError, match="test audit outage"):
            await client.post(f"/api/v1/orders/{identifier}/cancel", json=request_body())
        cancel.assert_not_awaited()
        assert "PAPER_ORDER_CANCELLATION_PENDING" in engine.gate.reason()
    finally:
        await worker.stop()
        await client.aclose()


async def test_duplicate_concurrent_owner_cancel_is_one_broker_call(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    cancel = AsyncMock(wraps=engine.cancel)
    monkeypatch.setattr(engine, "cancel", cancel)
    body = request_body()
    try:
        responses = await asyncio.gather(
            *[client.post(f"/api/v1/orders/{identifier}/cancel", json=body) for _ in range(2)]
        )
        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json() == responses[1].json()
        cancel.assert_awaited_once_with(identifier)
    finally:
        await worker.stop()
        await client.aclose()


async def test_owner_broker_failure_returns_unresolved_then_supervisor_recovers(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                engine.broker,
                "cancel_order",
                AsyncMock(side_effect=RuntimeError("test broker outage")),
            )
            response = await client.post(f"/api/v1/orders/{identifier}/cancel", json=request_body())
        assert response.status_code == 200, response.text
        assert not response.json()["terminal"]
        assert "PAPER_ORDER_CANCELLATION_PENDING" in engine.gate.reason()
        assert await PaperOrderHygiene(engine).run()
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("case", ["confirmation", "mode", "exit", "worker", "terminal"])
async def test_unsafe_cancel_refused_before_broker(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, case
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    cancel = AsyncMock(wraps=engine.cancel)
    monkeypatch.setattr(engine, "cancel", cancel)
    body = request_body()
    if case == "confirmation":
        body["confirmation"] = "wrong"
    elif case == "mode":
        engine.settings.trading_mode = TradingMode.SUPERVISED
    elif case == "worker":
        monkeypatch.setattr(orders_api, "active_worker", lambda: None)
    else:
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            if case == "exit":
                order.role = "EXIT"
            else:
                order.status = OrderStatus.CANCELLED
    try:
        response = await client.post(f"/api/v1/orders/{identifier}/cancel", json=body)
        assert response.status_code == 409, response.text
        cancel.assert_not_awaited()
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "OWNER_CANCEL_REFUSED")
                )
                is not None
            )
    finally:
        await worker.stop()
        await client.aclose()


async def test_cancel_requires_authentication(db_engine):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/orders/unknown/cancel", json=request_body())
    assert response.status_code == 401


async def test_owner_intent_recovers_after_result_storage_failure(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    engine, identifier, client, worker = await prepare(
        credentials, fake_clock, tmp_path, monkeypatch
    )
    original = PaperOrderHygiene.record

    async def interrupted(self, order, chain, event, result, **kwargs):
        if event == "ENTRY_CANCEL_RESULT":
            raise RuntimeError("isolated result storage failure")
        return await original(self, order, chain, event, result, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(PaperOrderHygiene, "record", interrupted)
            with pytest.raises(RuntimeError, match="isolated result storage failure"):
                await client.post(f"/api/v1/orders/{identifier}/cancel", json=request_body())
        await worker.stop()
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        assert await PaperOrderHygiene(restored).run()
        await restored.verify_protection()
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
            intents = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_INTENT")
                )
            )
        assert len(intents) == 1
        assert intents[0].actor == "owner"
        assert await AuditService().verify(intents[0].chain_id, expected_count=2)
        assert len(await restored.broker.list_orders()) == 1
    finally:
        await worker.stop()
        await client.aclose()
