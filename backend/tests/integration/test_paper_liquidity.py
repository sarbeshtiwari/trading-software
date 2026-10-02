"""Actual durable PAPER fills cannot replenish a consumed snapshot by polling."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.paper.engine import FillConfig
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.enums import OrderStatus, Product, Segment, TransactionType
from app.core.errors import ValidationError
from tests.integration.test_paper_broker import _quote, _request


async def test_depth_is_shared_across_products_cancel_polling_and_restart(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    quote = _quote("100", asks=(("100", 50), ("101", 50)), clock=fake_clock)

    async def source(reference):
        return quote

    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        fill_config=FillConfig(latency_ms=0),
        state_store=PaperStateStore(),
    )
    first = await broker.place_order(_request(quantity=80))
    second = await broker.place_order(
        _request(quantity=80, reference_id="PAPER000002", product=Product.CNC)
    )
    assert first.status == OrderStatus.EXECUTED
    assert second.status == OrderStatus.PARTIALLY_FILLED
    first_fills = await broker.list_trades(first.broker_order_id, Segment.CASH)
    second_fills = await broker.list_trades(second.broker_order_id, Segment.CASH)
    assert [(fill.quantity, fill.price) for fill in first_fills] == [
        (50, Decimal(100)),
        (30, Decimal(101)),
    ]
    assert [(fill.quantity, fill.price) for fill in second_fills] == [(20, Decimal(101))]
    await broker.cancel_order(second.broker_order_id, Segment.CASH)
    third = await broker.place_order(_request(quantity=40, reference_id="PAPER000003"))
    assert third.status == OrderStatus.OPEN
    await broker.settle_open_orders()
    assert await broker.list_trades(third.broker_order_id, Segment.CASH) == []
    await broker.close()
    restored = PaperBrokerProvider(
        settings, clock=fake_clock, quote_source=source, state_store=PaperStateStore()
    )
    await restored.restore()
    await restored.settle_open_orders()
    assert await restored.list_trades(third.broker_order_id, Segment.CASH) == []
    fake_clock.advance(timedelta(seconds=1))
    quote = replace(quote, observed_at=fake_clock.now())
    await restored.settle_open_orders()
    fills = await restored.list_trades(third.broker_order_id, Segment.CASH)
    assert [(fill.quantity, fill.price) for fill in fills] == [(40, Decimal(100))]
    assert (
        fills[0].raw["liquidity"]["observed_at"] != first_fills[0].raw["liquidity"]["observed_at"]
    )
    assert (
        fills[0].raw["liquidity"]["book_sha256"] == first_fills[0].raw["liquidity"]["book_sha256"]
    )
    assert fills[0].raw["liquidity"]["mode"] == "DEPTH"


async def test_buy_and_sell_depth_are_independent_and_rejection_consumes_nothing(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    quote = _quote("100", bids=(("99", 50),), asks=(("100", 50),), clock=fake_clock)

    async def source(reference):
        return quote

    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0, reject_probability=0.5, seed=1),
    )
    rejected = await broker.place_order(_request(quantity=50))
    bought = await broker.place_order(_request(quantity=50, reference_id="PAPER000002"))
    quote = replace(quote, bids=(replace(quote.bids[0], price=Decimal(98)),))
    sold = await broker.place_order(
        _request(quantity=50, reference_id="PAPER000003", transaction_type=TransactionType.SELL)
    )
    assert rejected.status == OrderStatus.REJECTED
    assert bought.status == sold.status == OrderStatus.EXECUTED
    assert await broker.list_trades(rejected.broker_order_id, Segment.CASH) == []
    assert (await broker.list_trades(bought.broker_order_id, Segment.CASH))[0].price == 100
    assert (await broker.list_trades(sold.broker_order_id, Segment.CASH))[0].price == 98


async def test_same_timestamp_conflict_and_regressed_book_fail_closed(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    quote = _quote("100", asks=(("100", 10),), clock=fake_clock)
    original = quote

    async def source(reference):
        return quote

    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    ack = await broker.place_order(_request(quantity=50))
    quote = replace(quote, asks=(replace(quote.asks[0], quantity=100),))
    with pytest.raises(ValidationError, match="Conflicting PAPER depth"):
        await broker.settle_open_orders()
    assert (
        sum(fill.quantity for fill in await broker.list_trades(ack.broker_order_id, Segment.CASH))
        == 10
    )
    fake_clock.advance(timedelta(seconds=1))
    quote = replace(original, observed_at=fake_clock.now())
    await broker.settle_open_orders()
    quote = original
    with pytest.raises(ValidationError, match="observation regressed"):
        await broker.settle_open_orders()
    assert (
        sum(fill.quantity for fill in await broker.list_trades(ack.broker_order_id, Segment.CASH))
        == 20
    )


@pytest.mark.parametrize("corrupt", [False, True])
async def test_legacy_or_altered_fill_evidence_does_not_restore_free_capacity(
    db_engine, settings_env, fake_clock, corrupt
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    quote = _quote("100", asks=(("100", 10),), clock=fake_clock)

    async def source(reference):
        return quote

    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        state_store=store,
        fill_config=FillConfig(latency_ms=0),
    )
    ack = await broker.place_order(_request(quantity=50))
    saved = broker.snapshot()
    if corrupt:
        saved["trades"][0]["raw"]["liquidity"]["book"]["asks"][0]["quantity"] = 100
    else:
        saved["trades"][0]["raw"].pop("liquidity")
    await store.save(saved)
    restored = PaperBrokerProvider(
        settings, clock=fake_clock, quote_source=source, state_store=PaperStateStore()
    )
    await restored.restore()
    with pytest.raises(
        ValidationError, match="integrity invalid" if corrupt else "Legacy PAPER fill"
    ):
        await restored.settle_open_orders()
    assert (
        sum(fill.quantity for fill in await restored.list_trades(ack.broker_order_id, Segment.CASH))
        == 10
    )
    if not corrupt:
        fake_clock.advance(timedelta(seconds=1))
        quote = replace(quote, observed_at=fake_clock.now())
        await restored.settle_open_orders()
        assert (
            sum(
                fill.quantity
                for fill in await restored.list_trades(ack.broker_order_id, Segment.CASH)
            )
            == 20
        )


async def test_pending_priority_survives_reordered_durable_json(
    db_engine, settings_env, fake_clock
):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    quote = _quote("100", asks=(("100", 50),), clock=fake_clock)

    async def source(reference):
        return quote

    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        state_store=store,
        fill_config=FillConfig(latency_ms=150),
    )
    first = await broker.place_order(_request(quantity=50))
    second = await broker.place_order(_request(quantity=50, reference_id="PAPER000002"))
    assert first.status == second.status == OrderStatus.OPEN
    saved = broker.snapshot()
    saved["orders"] = dict(reversed(list(saved["orders"].items())))
    await store.save(saved)
    restored = PaperBrokerProvider(
        settings, clock=fake_clock, quote_source=source, state_store=PaperStateStore()
    )
    await restored.restore()
    fake_clock.advance(timedelta(milliseconds=150))
    await restored.settle_open_orders()
    assert sum(
        fill.quantity for fill in await restored.list_trades(first.broker_order_id, Segment.CASH)
    ) == 50
    assert await restored.list_trades(second.broker_order_id, Segment.CASH) == []
