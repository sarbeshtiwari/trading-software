"""Supplied fixture circuit bands reach real OMS preflight and PAPER fills."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, OrderStatus, Segment
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.execution.paper import PaperExecution
from app.marketdata.models import InstrumentRef
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


async def test_market_fed_worker_refuses_missing_bands(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True, origin=DataOrigin.LIVE
    )
    provider.get_quote.return_value = replace(
        provider.get_quote.return_value, lower_circuit=None, upper_circuit=None
    )
    submit = AsyncMock(wraps=worker.executor.broker.place_order)
    monkeypatch.setattr(worker.executor.broker, "place_order", submit)
    try:
        await worker.cycle()
        submit.assert_not_awaited()
        assert await worker.executor.broker.list_orders() == []
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.event_type == "ENTRY_CIRCUIT_PREFLIGHT",
                )
            )
            assert event is not None
            assert event.result["status"] == "UNAVAILABLE"
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize(
    "lower,upper,code",
    [
        ("90", "99.95", "PRICE_OUTSIDE_CIRCUIT_BAND"),
        ("101", "110", "PRICE_OUTSIDE_CIRCUIT_BAND"),
        ("110", "90", "INVALID_CIRCUIT_BAND"),
        (None, "110", "INVALID_CIRCUIT_BAND"),
    ],
)
async def test_preflight_circuit_rejection_never_calls_broker(
    db_engine, credentials, fake_clock, monkeypatch, lower, upper, code
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    submit = AsyncMock(wraps=engine.broker.place_order)
    monkeypatch.setattr(engine.broker, "place_order", submit)
    market.update(lower_circuit=Decimal(lower) if lower else None, upper_circuit=Decimal(upper))
    try:
        with pytest.raises(SafetyError, match=code):
            await engine.submit(proposal)
        submit.assert_not_awaited()
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.event_type == "ENTRY_CIRCUIT_PREFLIGHT",
                    AuditEvent.proposal_id == proposal,
                )
            )
            assert event.result["status"] == code
            assert event.result["upper"] == upper
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", ["band", "stale", "missing"])
async def test_dispatch_refresh_blocks_and_remains_rejected_after_restart(
    db_engine, credentials, fake_clock, monkeypatch, change
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market.update(lower_circuit=Decimal("90"), upper_circuit=Decimal("110"))
    source = engine.source
    calls = 0

    async def changing_source(instrument):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "band":
                market["upper_circuit"] = Decimal("99.95")
            elif change == "stale":
                fake_clock.advance_seconds(60)
            else:
                return None
        return await source(instrument)

    engine.source = changing_source
    submit = AsyncMock(wraps=engine.broker.place_order)
    monkeypatch.setattr(engine.broker, "place_order", submit)
    try:
        with pytest.raises(SafetyError):
            await engine.submit(proposal)
        submit.assert_not_awaited()
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.status == OrderStatus.REJECTED
            assert order.rejection_reason
        restored = PaperExecution(source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        assert await restored.submit(proposal) == order.id
        assert await restored.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_live_source_missing_bands_are_not_authorized(db_engine, credentials, fake_clock):
    engine, identifier, _market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, identifier)
        quote = await engine.source(InstrumentRef("TEST", Exchange.NSE, Segment.CASH))
        with pytest.raises(SafetyError, match="LIVE_SOURCE_CIRCUIT_BAND_UNAVAILABLE"):
            await engine._circuit_preflight(proposal, replace(quote, data_origin=DataOrigin.LIVE))
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_exact_circuit_boundaries_allow_real_paper_fill(db_engine, credentials, fake_clock):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market.update(lower_circuit=Decimal("99.95"), upper_circuit=Decimal("100"))
    try:
        await engine.submit(proposal)
        orders = await engine.broker.list_orders()
        assert len(orders) == 1
        assert orders[0].filled_quantity == 250
    finally:
        await client.aclose()
