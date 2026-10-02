"""Clock-driven submission eligibility survives durable PAPER broker recovery."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.models import ModifyRequest
from app.brokers.paper.engine import FillConfig
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.enums import OrderStatus, OrderType, Segment
from app.core.errors import ValidationError
from tests.integration.test_paper_broker import _quote, _request


async def test_default_latency_survives_restart_and_uses_execution_time_quote(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    start = fake_clock.now()

    async def quote_source(reference):
        price = "250" if fake_clock.now() < start + timedelta(milliseconds=150) else "252"
        return _quote(price, asks=((price, 500),), clock=fake_clock)

    first = PaperBrokerProvider(
        settings, clock=fake_clock, state_store=PaperStateStore(), quote_source=quote_source
    )
    ack = await first.place_order(_request())
    assert ack.status == OrderStatus.OPEN
    assert first.next_execution_at == start + timedelta(milliseconds=150)
    assert await first.list_trades(ack.broker_order_id, Segment.CASH) == []
    assert (await first.place_order(_request())).broker_order_id == ack.broker_order_id
    await first.close()
    fake_clock.advance(timedelta(milliseconds=149))
    restored = PaperBrokerProvider(
        settings, clock=fake_clock, state_store=PaperStateStore(), quote_source=quote_source
    )
    await restored.restore()
    await restored.settle_open_orders()
    assert await restored.list_trades(ack.broker_order_id, Segment.CASH) == []
    fake_clock.advance(timedelta(milliseconds=1))
    await restored.settle_open_orders()
    fills = await restored.list_trades(ack.broker_order_id, Segment.CASH)
    assert len(fills) == 1 and fills[0].price == 252
    assert fills[0].executed_at == start + timedelta(milliseconds=150)
    assert restored.next_execution_at is None
    await restored.settle_open_orders()
    assert len(await restored.list_trades(ack.broker_order_id, Segment.CASH)) == 1


async def test_modify_restarts_latency_and_cancel_prevents_late_fill(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")

    async def quote_source(reference):
        return _quote("250", asks=(("250", 500),), clock=fake_clock)

    broker = PaperBrokerProvider(
        settings, clock=fake_clock, state_store=PaperStateStore(), quote_source=quote_source
    )
    ack = await broker.place_order(_request(order_type=OrderType.LIMIT, price=Decimal(249)))
    fake_clock.advance(timedelta(milliseconds=100))
    changed_at = fake_clock.now()
    await broker.modify_order(
        ModifyRequest(broker_order_id=ack.broker_order_id, segment=Segment.CASH, price=Decimal(250))
    )
    assert broker.next_execution_at == changed_at + timedelta(milliseconds=150)
    fake_clock.advance(timedelta(milliseconds=50))
    await broker.settle_open_orders()
    assert await broker.list_trades(ack.broker_order_id, Segment.CASH) == []
    await broker.cancel_order(ack.broker_order_id, Segment.CASH)
    fake_clock.advance(timedelta(seconds=1))
    await broker.settle_open_orders()
    assert (
        await broker.get_order(ack.broker_order_id, Segment.CASH)
    ).status == OrderStatus.CANCELLED
    assert await broker.list_trades(ack.broker_order_id, Segment.CASH) == []


async def test_missing_creation_and_eligibility_refuses_execution(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    store = PaperStateStore()
    broker = PaperBrokerProvider(settings, clock=fake_clock, state_store=store)
    ack = await broker.place_order(_request())
    snapshot = broker.snapshot()
    snapshot["orders"][ack.broker_order_id].update(created_at=None, eligible_at=None)
    await store.save(snapshot)
    restored = PaperBrokerProvider(settings, clock=fake_clock, state_store=PaperStateStore())
    await restored.restore()
    with pytest.raises(ValidationError, match="creation time unavailable"):
        await restored.settle_open_orders()
    assert await restored.list_trades(ack.broker_order_id, Segment.CASH) == []
    with pytest.raises(ValueError, match="nonnegative integer"):
        FillConfig(latency_ms=-1)


async def test_elapsed_latency_does_not_authorize_future_market_data(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    future = fake_clock.now() + timedelta(seconds=1)
    quote = replace(_quote("250", asks=(("250", 500),), clock=fake_clock), observed_at=future)

    async def quote_source(reference):
        return quote

    broker = PaperBrokerProvider(
        settings, clock=fake_clock, state_store=PaperStateStore(), quote_source=quote_source
    )
    ack = await broker.place_order(_request())
    fake_clock.advance(timedelta(milliseconds=150))
    await broker.settle_open_orders()
    assert await broker.list_trades(ack.broker_order_id, Segment.CASH) == []
    assert (await broker.get_order(ack.broker_order_id, Segment.CASH)).status == OrderStatus.OPEN
    fake_clock.set_to(future)
    await broker.settle_open_orders()
    fills = await broker.list_trades(ack.broker_order_id, Segment.CASH)
    assert len(fills) == 1 and fills[0].executed_at == future


@pytest.mark.parametrize("price_jump", [False, True])
async def test_pending_orders_cannot_overdraw_margin_at_execution(
    db_engine, settings_env, fake_clock, price_jump
):
    settings = settings_env(STARTING_CAPITAL="1000", TRADING_MODE="PAPER")
    start = fake_clock.now()

    async def quote_source(reference):
        price = "100" if price_jump and fake_clock.now() > start else "40"
        return _quote(price, asks=((price, 500),), clock=fake_clock)

    broker = PaperBrokerProvider(
        settings, clock=fake_clock, state_store=PaperStateStore(), quote_source=quote_source
    )
    first = await broker.place_order(_request())
    second = await broker.place_order(_request(reference_id="PAPER000002"))
    assert first.status == second.status == OrderStatus.OPEN
    fake_clock.advance(timedelta(milliseconds=150))
    await broker.settle_open_orders()
    first_order = await broker.get_order(first.broker_order_id, Segment.CASH)
    second_order = await broker.get_order(second.broker_order_id, Segment.CASH)
    assert first_order.status == (OrderStatus.REJECTED if price_jump else OrderStatus.EXECUTED)
    assert second_order.status == OrderStatus.REJECTED
    assert await broker.list_trades(second.broker_order_id, Segment.CASH) == []
    margin = await broker.get_margin()
    assert margin.available_margin == (Decimal(1000) if price_jump else Decimal(200))
