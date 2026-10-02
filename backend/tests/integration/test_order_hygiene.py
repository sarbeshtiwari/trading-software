"""Actual partial PAPER entries expire without cancelling protection or refilling."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.calendar import TradingCalendar
from app.core.enums import ExitReason, OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order, Position
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from app.trading.worker import PaperWorker
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def test_cancel_result_recovers_after_storage_interruption(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    hygiene = PaperOrderHygiene(engine)
    original = hygiene.record

    async def interrupted(order, chain, event, result):
        if event == "ENTRY_CANCEL_RESULT":
            raise RuntimeError("isolated result storage outage")
        return await original(order, chain, event, result)

    monkeypatch.setattr(hygiene, "record", interrupted)
    try:
        with pytest.raises(RuntimeError, match="result storage outage"):
            await hygiene.run(all_pending=True)
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        assert await PaperOrderHygiene(restored).run()
        assert await PaperOrderHygiene(restored).run()
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
            intents = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_INTENT")
                )
            )
            results = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
                )
            )
        assert len(intents) == len(results) == 1
        assert intents[0].chain_id == results[0].chain_id
        assert results[0].result["reason"] == "SESSION_CUTOFF"
        assert len(await restored.broker.list_orders()) == 1
    finally:
        await client.aclose()


async def test_pending_exit_is_not_swept_and_future_entry_fails_closed(
    db_engine, credentials, fake_clock
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        market["quantity"] = 100
        exit_id = await engine.exit(position.id, ExitReason.EMERGENCY)
        assert await PaperOrderHygiene(engine).run(all_pending=True)
        async with db_session.session_scope() as session:
            assert (await session.get(Order, exit_id)).status == OrderStatus.PARTIALLY_FILLED
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "ENTRY_CANCEL_INTENT")
                )
                == 0
            )
            entry = await session.get(Order, identifier)
            entry.status = OrderStatus.UNKNOWN
            entry.submitted_at = fake_clock.utcnow() + timedelta(seconds=1)
        assert not await PaperOrderHygiene(engine).run()
        async with db_session.session_scope() as session:
            result = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
            )
        assert result.result["error"] == "FUTURE_ORDER_TIMESTAMP"
    finally:
        await client.aclose()


async def test_sweep_does_not_cancel_without_durable_intent(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    cancel = AsyncMock(wraps=engine.cancel)
    monkeypatch.setattr(engine, "cancel", cancel)
    monkeypatch.setattr(
        AuditService, "append_in_session", AsyncMock(side_effect=RuntimeError("isolated audit outage"))
    )
    try:
        with pytest.raises(RuntimeError, match="isolated audit outage"):
            await PaperOrderHygiene(engine).run(all_pending=True)
        cancel.assert_not_awaited()
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.PARTIALLY_FILLED
    finally:
        await client.aclose()


async def test_stale_partial_entry_cancelled_by_worker_and_restart(
    db_engine, credentials, fake_clock, tmp_path
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    engine.settings.paper_entry_max_age_seconds = 10
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    worker = PaperWorker(
        engine, calendar=TradingCalendar(complete_years=[2026]), lock_path=tmp_path / "hygiene.lock"
    )
    try:
        await worker.start(schedule=False)
        fake_clock.advance(timedelta(seconds=9))
        assert await PaperOrderHygiene(engine).run()
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.PARTIALLY_FILLED
        fake_clock.advance(timedelta(seconds=1))
        market["observed"] = fake_clock.now()
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            position = await session.scalar(sa.select(Position))
            assert order.status == OrderStatus.CANCELLED
            assert position.net_quantity == 100
            records = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
                )
            )
        assert len(records) == 1
        assert records[0].result["reason"] == "STALE_ENTRY"
        assert records[0].result["filled_quantity"] == 100
        assert await AuditService().verify(records[0].chain_id, expected_count=2)
        await worker.stop()
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        assert await PaperOrderHygiene(restored).run()
        assert len(await restored.broker.list_orders()) == 1
        await restored.verify_protection()
    finally:
        await worker.stop()
        await client.aclose()


async def test_failed_cancel_records_unknown_and_retries_lookup_without_duplicate(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                engine.broker,
                "cancel_order",
                AsyncMock(side_effect=RuntimeError("isolated cancel outage")),
            )
            assert not await PaperOrderHygiene(engine).run(all_pending=True)
        assert "PAPER_ORDER_CANCELLATION_UNRESOLVED" in engine.gate.reason()
        assert await PaperOrderHygiene(engine).run(all_pending=True)
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
            outcomes = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
                )
            )
        assert len(outcomes) == 2
        assert any(not row.result["terminal"] for row in outcomes)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()
