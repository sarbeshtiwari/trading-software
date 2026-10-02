"""Instrument metadata reaches durable PAPER fills through production resolvers."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.models import ModifyRequest
from app.brokers.paper.constraints import database_constraints
from app.brokers.paper.engine import FillConfig
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.enums import Exchange, InstrumentType, OrderStatus, Segment, TransactionType
from app.db import session as db_session
from app.db.models.instrument import Instrument
from tests.integration.test_paper_broker import _quote, _request


async def contract(*, lot=25, tick="0.05"):
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="fixture-contract",
                exchange=Exchange.NSE,
                segment=Segment.FNO,
                instrument_type=InstrumentType.FUTURE,
                trading_symbol="FIXTURE-FUT",
                lot_size=lot,
                tick_size=Decimal(tick),
                source="DETERMINISTIC_TEST",
            )
        )


def request(**changes):
    return _request(trading_symbol="FIXTURE-FUT", segment=Segment.FNO, **changes)


def quote(fake_clock, **changes):
    return _quote("100", symbol="FIXTURE-FUT", segment=Segment.FNO, clock=fake_clock, **changes)


async def test_partial_lots_snapshot_constraints_survive_restart(
    db_engine, settings_env, fake_clock
):
    await contract()
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    market = quote(fake_clock, asks=(("100", 60), ("100.05", 24)))

    async def source(reference):
        return market

    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    ack = await broker.place_order(request(quantity=100))
    assert ack.status == OrderStatus.PARTIALLY_FILLED
    fills = await broker.list_trades(ack.broker_order_id, Segment.FNO)
    assert [(fill.quantity, fill.price) for fill in fills] == [(50, Decimal(100))]
    assert fills[0].raw["instrument_constraints"]["lot_size"] == 25
    await broker.settle_open_orders()
    assert len(await broker.list_trades(ack.broker_order_id, Segment.FNO)) == 1
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "fixture-contract")
        instrument.lot_size = 100
        instrument.tick_size = Decimal(1)
    restored = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
    )
    await restored.restore()
    fake_clock.advance(timedelta(seconds=1))
    market = quote(fake_clock, asks=(("100.05", 75),))
    await restored.settle_open_orders()
    fills = await restored.list_trades(ack.broker_order_id, Segment.FNO)
    assert [(fill.quantity, fill.price) for fill in fills] == [
        (50, Decimal(100)),
        (50, Decimal("100.05")),
    ]
    assert all(fill.raw["instrument_constraints"]["lot_size"] == 25 for fill in fills)


@pytest.mark.parametrize("case", ["missing", "quantity", "depth_tick", "modify", "legacy"])
async def test_unsafe_contract_requests_never_create_fills(
    db_engine, settings_env, fake_clock, case
):
    if case != "missing":
        await contract()
    market = quote(fake_clock, asks=(("100.03" if case == "depth_tick" else "100", 100),))

    async def source(reference):
        return market

    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=store,
        fill_config=FillConfig(latency_ms=150 if case in {"modify", "legacy"} else 0),
    )
    ack = await broker.place_order(request(quantity=51 if case == "quantity" else 100))
    if case == "modify":
        await broker.modify_order(ModifyRequest(ack.broker_order_id, Segment.FNO, quantity=51))
        fake_clock.advance(timedelta(milliseconds=150))
        await broker.settle_open_orders()
    elif case == "legacy":
        saved = broker.snapshot()
        saved["orders"][ack.broker_order_id].pop("constraints")
        await store.save(saved)
        broker = PaperBrokerProvider(
            settings,
            clock=fake_clock,
            quote_source=source,
            constraint_source=database_constraints,
            state_store=store,
        )
        await broker.restore()
        fake_clock.advance(timedelta(milliseconds=150))
        await broker.settle_open_orders()
    order = await broker.get_order(ack.broker_order_id, Segment.FNO)
    assert order.status == OrderStatus.REJECTED
    assert await broker.list_trades(ack.broker_order_id, Segment.FNO) == []
    assert broker.account.cash == Decimal(500000)


@pytest.mark.parametrize(
    "side,expected", [(TransactionType.BUY, "100.15"), (TransactionType.SELL, "99.85")]
)
async def test_partial_fallback_is_adverse_tick_and_whole_lots(
    db_engine, settings_env, fake_clock, side, expected
):
    await contract()
    market = quote(fake_clock)

    async def source(reference):
        return market

    broker = PaperBrokerProvider(
        settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER"),
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(
            latency_ms=0, slippage_bps=Decimal(11), partial_fill_probability=1, seed=1
        ),
    )
    ack = await broker.place_order(request(quantity=100, transaction_type=side))
    fills = await broker.list_trades(ack.broker_order_id, Segment.FNO)
    assert [(fill.quantity, fill.price) for fill in fills] == [(75, Decimal(expected))]
    assert ack.status == OrderStatus.PARTIALLY_FILLED


async def test_sub_paise_ticks_preserve_original_depth_price_and_capacity(
    db_engine, settings_env, fake_clock
):
    await contract(lot=1, tick="0.0005")
    market = quote(fake_clock, asks=(("100.0005", 15),))
    market = replace(market, ltp=Decimal("100.0005"))

    async def source(reference):
        return market

    broker = PaperBrokerProvider(
        settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER"),
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    first = await broker.place_order(request(quantity=10))
    second = await broker.place_order(request(quantity=10, reference_id="PAPER000002"))
    fills = [
        *(await broker.list_trades(first.broker_order_id, Segment.FNO)),
        *(await broker.list_trades(second.broker_order_id, Segment.FNO)),
    ]
    assert [(fill.quantity, fill.price) for fill in fills] == [
        (10, Decimal("100.0005")),
        (5, Decimal("100.0005")),
    ]
