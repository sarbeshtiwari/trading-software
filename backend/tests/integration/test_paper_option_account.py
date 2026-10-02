"""Typed option fills reserve premium in the real PAPER broker; OMS enablement is separate."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.paper.account import PaperAccount
from app.brokers.paper.constraints import database_constraints
from app.brokers.paper.engine import FillConfig
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.data_origin import DataOrigin
from app.core.enums import (
    InstrumentType,
    OptionType,
    OrderStatus,
    Product,
    Segment,
    TransactionType,
)
from app.core.errors import ValidationError
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.paper import PAPER_STATE_ID, PaperBrokerState
from app.portfolio.cost_store import CostStore
from tests.integration.test_paper_broker import _quote, _request
from tests.integration.test_paper_constraints import contract
from tests.unit.test_costs import schedule


async def option_contract(clock):
    await contract()
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "fixture-contract")
        instrument.instrument_type = InstrumentType.OPTION
        instrument.trading_symbol = "FIXTURE-CE"
        instrument.expiry_date = clock.now().date() + timedelta(days=7)
        instrument.option_type = OptionType.CE
        instrument.strike_price = Decimal(1000)
        instrument.underlying = "FIXTURE"


async def test_explicit_option_tariff_reaches_partial_fills_and_restart(
    db_engine, settings_env, fake_clock
):
    await option_contract(fake_clock)
    store = CostStore(fake_clock)
    tariff = schedule(
        segment=Segment.FNO,
        charge_basis="OPTION_PREMIUM",
        known_at=fake_clock.now(),
        effective_from=fake_clock.now(),
        effective_to=fake_clock.now() + timedelta(days=1),
    )
    await store.publish(tariff, actor="test-owner", reason="Synthetic option fee fixture")
    assert await store.for_order(request(), fake_clock.now() - timedelta(seconds=1)) is None
    assert await store.for_order(request(trading_symbol="UNKNOWN"), fake_clock.now()) is None
    settings = settings_env(STARTING_CAPITAL="20000", TRADING_MODE="PAPER")
    market = quote(fake_clock, "100", quantity=50)

    async def source(reference):
        return market

    def broker():
        return PaperBrokerProvider(
            settings,
            clock=fake_clock,
            quote_source=source,
            constraint_source=database_constraints,
            cost_source=store.for_order,
            state_store=PaperStateStore(),
            fill_config=FillConfig(latency_ms=0),
        )

    current = broker()
    entry = await current.place_order(request(quantity=100))
    assert entry.status == OrderStatus.PARTIALLY_FILLED
    current = broker()
    await current.restore()
    fake_clock.advance(timedelta(seconds=1))
    market = quote(fake_clock, "100", quantity=50)
    await current.settle_open_orders()
    assert current.account.charges_paid == Decimal("12.48")
    assert current.account.used_margin == 10000
    market = quote(fake_clock, "110", quantity=100)
    exit_order = await current.place_order(
        request(
            reference_id="OPTION-COST-EXIT",
            quantity=100,
            transaction_type=TransactionType.SELL,
        )
    )
    assert exit_order.status == OrderStatus.EXECUTED
    assert current.account.realised_pnl == 1000
    assert current.account.charges_paid == Decimal("28.62")
    assert current.account.cash == Decimal("20971.38")
    assert current.account.used_margin == 0
    current = broker()
    await current.restore()
    assert current.account.cash == Decimal("20971.38")
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "fixture-contract")
        instrument.instrument_type = InstrumentType.FUTURE
    assert await store.for_order(request(), fake_clock.now()) is None


def request(**changes):
    return _request(**({"trading_symbol": "FIXTURE-CE", "segment": Segment.FNO} | changes))


@pytest.mark.parametrize("wrong_scope", [True, False])
async def test_fee_scope_cannot_be_relabelled_by_cost_source(
    db_engine, settings_env, fake_clock, wrong_scope
):
    await option_contract(fake_clock)
    if not wrong_scope:
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, "fixture-contract")
            instrument.instrument_type = InstrumentType.FUTURE

    async def fees(order, as_of):
        return schedule(
            segment=Segment.CASH if wrong_scope else Segment.FNO,
            charge_basis="CASH_TURNOVER" if wrong_scope else "OPTION_PREMIUM",
            known_at=as_of,
            effective_from=as_of,
            effective_to=as_of + timedelta(days=1),
        )

    async def source(reference):
        return quote(fake_clock, "100")

    broker = PaperBrokerProvider(
        settings_env(STARTING_CAPITAL="20000", TRADING_MODE="PAPER"),
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        cost_source=fees,
        fill_config=FillConfig(latency_ms=0),
    )
    with pytest.raises(ValidationError, match=r"scope|identity"):
        await broker.place_order(request(quantity=25))
    assert broker.account.cash == 20000
    assert broker.account.charges_paid == 0
    assert broker.account.open_positions() == []


def quote(clock, price, *, quantity=100):
    return replace(
        _quote(
            price,
            symbol="FIXTURE-CE",
            segment=Segment.FNO,
            clock=clock,
            bids=((price, quantity),),
            asks=((price, quantity),),
        ),
        data_origin=DataOrigin.SYNTHETIC,
    )


async def test_premium_fifo_partial_fills_restart_and_no_naked_reversal(
    db_engine,
    settings_env,
    fake_clock,
):
    await option_contract(fake_clock)
    settings = settings_env(STARTING_CAPITAL="20000", TRADING_MODE="PAPER")
    market = quote(fake_clock, "100", quantity=50)

    async def source(reference):
        return market

    def broker():
        return PaperBrokerProvider(
            settings,
            clock=fake_clock,
            quote_source=source,
            constraint_source=database_constraints,
            state_store=PaperStateStore(),
            fill_config=FillConfig(latency_ms=0),
        )

    current = broker()
    entry = await current.place_order(request(quantity=100))
    assert entry.status == OrderStatus.PARTIALLY_FILLED
    assert current.account.used_margin == 5000
    assert current.account.available_margin == 15000
    current = broker()
    await current.restore()
    assert current.account.open_positions()[0].instrument_type == InstrumentType.OPTION
    fake_clock.advance(timedelta(seconds=1))
    market = quote(fake_clock, "101", quantity=50)
    await current.settle_open_orders()
    assert (
        await current.get_order(entry.broker_order_id, Segment.FNO)
    ).status == OrderStatus.EXECUTED
    assert current.account.used_margin == 10050
    assert current.account.available_margin == 9950
    for fill in await current.list_trades(entry.broker_order_id, Segment.FNO):
        assert fill.raw["instrument_constraints"]["instrument_type"] == "OPTION"
    fake_clock.advance(timedelta(seconds=1))
    market = quote(fake_clock, "110")
    partial = await current.place_order(
        request(
            reference_id="OPTION-EXIT-1",
            quantity=75,
            transaction_type=TransactionType.SELL,
        )
    )
    assert partial.status == OrderStatus.EXECUTED
    assert current.account.realised_pnl == 725
    assert current.account.used_margin == 2525
    current = broker()
    await current.restore()
    assert current.account.used_margin == 2525
    oversized = request(
        reference_id="OPTION-NAKED", quantity=50, transaction_type=TransactionType.SELL
    )
    rejected = await current.place_order(oversized)
    assert rejected.status == OrderStatus.REJECTED
    assert (
        await current.get_order(rejected.broker_order_id, Segment.FNO)
    ).rejection_reason == "NAKED_SHORT_DISABLED"
    assert (await current.place_order(oversized)).broker_order_id == rejected.broker_order_id
    assert await current.list_trades(rejected.broker_order_id, Segment.FNO) == []
    await current.place_order(
        request(
            reference_id="OPTION-EXIT-2",
            quantity=25,
            transaction_type=TransactionType.SELL,
        )
    )
    assert current.account.used_margin == 0 and current.account.realised_pnl == 950
    assert current.account.cash == 20950 and current.account.open_positions() == []


async def test_option_premium_is_not_discounted_by_generic_fno_margin(
    db_engine,
    settings_env,
    fake_clock,
):
    await option_contract(fake_clock)

    async def source(reference):
        return quote(fake_clock, "100")

    broker = PaperBrokerProvider(
        settings_env(STARTING_CAPITAL="5000", TRADING_MODE="PAPER"),
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    rejected = await broker.place_order(request(quantity=100))
    assert rejected.status == OrderStatus.REJECTED
    assert await broker.list_trades(rejected.broker_order_id, Segment.FNO) == []
    assert broker.account.available_margin == 5000


async def test_restore_does_not_downgrade_option_identity(db_engine, settings_env, fake_clock):
    await option_contract(fake_clock)

    async def source(reference):
        return quote(fake_clock, "100")

    settings = settings_env(STARTING_CAPITAL="20000", TRADING_MODE="PAPER")
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    assert (await broker.place_order(request(quantity=25))).status == OrderStatus.EXECUTED
    async with db_session.session_scope() as session:
        row = await session.get(PaperBrokerState, PAPER_STATE_ID)
        positions = [{**position, "instrument_type": None} for position in row.account["positions"]]
        row.account = {**row.account, "positions": positions}
    restored = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
    )
    with pytest.raises(ValidationError, match="option identity"):
        await restored.restore()


def test_option_account_refuses_corrupt_margin_and_naked_fill_before_mutation():
    account = PaperAccount(Decimal(10000))
    assert account.requirement_for(
        1, Decimal("0.0001"), Product.MIS, Segment.FNO, InstrumentType.OPTION
    ) == Decimal("0.01")
    arguments = {
        "trading_symbol": "FIXTURE-CE",
        "segment": Segment.FNO,
        "product": Product.MIS,
        "quantity": 25,
        "price": Decimal(100),
        "instrument_type": InstrumentType.OPTION,
    }
    before = account.to_dict()
    with pytest.raises(ValidationError, match="NAKED_SHORT_DISABLED"):
        account.apply_fill(**arguments, transaction_type=TransactionType.SELL)
    assert account.to_dict() == before
    account.apply_fill(**arguments, transaction_type=TransactionType.BUY)
    persisted = account.to_dict()
    persisted["used_margin"] = "600"
    with pytest.raises(ValidationError, match="margin mismatch"):
        PaperAccount.from_dict(persisted)
    persisted = account.to_dict()
    persisted["positions"][0]["reserved_margin"] = "600"
    with pytest.raises(ValidationError, match="premium reservation"):
        PaperAccount.from_dict(persisted)


@pytest.mark.parametrize("condition", ["missing", "expired"])
async def test_option_contract_metadata_cannot_be_missing_or_expired(
    db_engine,
    settings_env,
    fake_clock,
    condition,
):
    await option_contract(fake_clock)
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "fixture-contract")
        instrument.expiry_date = (
            None if condition == "missing" else fake_clock.now().date() - timedelta(days=1)
        )

    async def source(reference):
        return quote(fake_clock, "100")

    broker = PaperBrokerProvider(
        settings_env(STARTING_CAPITAL="20000", TRADING_MODE="PAPER"),
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
        fill_config=FillConfig(latency_ms=0),
    )
    rejected = await broker.place_order(request(quantity=25))
    assert rejected.status == OrderStatus.REJECTED
    row = await broker.get_order(rejected.broker_order_id, Segment.FNO)
    assert row.rejection_reason == (
        "OPTION_CONTRACT_UNAVAILABLE" if condition == "missing" else "OPTION_CONTRACT_EXPIRED"
    )
    assert await broker.list_trades(rejected.broker_order_id, Segment.FNO) == []
    assert broker.account.available_margin == 20000
