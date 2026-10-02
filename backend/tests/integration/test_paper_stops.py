"""Stop activation is a durable event, not a condition reevaluated after reversal."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError as SchemaError

from app.brokers.models import ModifyRequest
from app.brokers.paper.constraints import database_constraints
from app.brokers.paper.engine import FillConfig
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.core.enums import OrderStatus, OrderType, Segment, TransactionType
from app.core.errors import ValidationError
from tests.integration.test_paper_constraints import contract, quote, request


@pytest.mark.parametrize("side", [TransactionType.BUY, TransactionType.SELL])
@pytest.mark.parametrize("kind", [OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET])
async def test_stop_activation_survives_partial_or_unfilled_restart_and_reversal(
    db_engine, settings_env, fake_clock, side, kind
):
    await contract()
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    buying = side == TransactionType.BUY
    initial = Decimal(100 if buying else 110)
    market = replace(quote(fake_clock), ltp=initial)

    async def source(reference):
        return market

    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=store,
        fill_config=FillConfig(latency_ms=0),
    )
    ack = await broker.place_order(
        request(
            quantity=100,
            transaction_type=side,
            order_type=kind,
            trigger_price=Decimal(105),
            price=Decimal(106 if buying else 104) if kind == OrderType.STOP_LOSS else None,
        )
    )
    assert ack.status == OrderStatus.OPEN
    fake_clock.advance(timedelta(seconds=1))
    level = "107" if buying else "103"
    market = replace(
        quote(
            fake_clock,
            asks=((level, 25),) if buying else (),
            bids=((level, 25),) if not buying else (),
        ),
        ltp=Decimal(105),
    )
    await broker.settle_open_orders()
    first_fills = await broker.list_trades(ack.broker_order_id, Segment.FNO)
    assert sum(fill.quantity for fill in first_fills) == (
        25 if kind == OrderType.STOP_LOSS_MARKET else 0
    )
    restored = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=PaperStateStore(),
    )
    await restored.restore()
    fake_clock.advance(timedelta(seconds=1))
    market = replace(
        quote(
            fake_clock,
            asks=((str(initial), 100),) if buying else (),
            bids=((str(initial), 100),) if not buying else (),
        ),
        ltp=initial,
    )
    await restored.settle_open_orders()
    fills = await restored.list_trades(ack.broker_order_id, Segment.FNO)
    assert sum(fill.quantity for fill in fills) == 100
    assert fills[-1].price == initial
    remote = await restored.get_order(ack.broker_order_id, Segment.FNO)
    assert remote.status == OrderStatus.EXECUTED
    activation = remote.raw["stop_activation"]
    assert Decimal(activation["ltp"]) == 105
    assert activation["transaction_type"] == side.value
    assert all(fill.raw["stop_activation"] == activation for fill in fills)


@pytest.mark.parametrize("case", ["latency", "future", "legacy", "modify", "cancel", "storage"])
async def test_stop_control_and_failure_boundaries(
    db_engine, settings_env, fake_clock, monkeypatch, case
):
    await contract()
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    market = quote(fake_clock, asks=(("107", 100),))

    async def source(reference):
        return market

    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=store,
        fill_config=FillConfig(latency_ms=150 if case == "latency" else 0),
    )
    ack = await broker.place_order(
        request(
            quantity=100,
            order_type=OrderType.STOP_LOSS,
            trigger_price=Decimal(105),
            price=Decimal(106),
        )
    )
    assert ack.status == OrderStatus.OPEN
    if case == "legacy":
        saved = broker.snapshot()
        saved["orders"][ack.broker_order_id].pop("stop_activation")
        saved["orders"][ack.broker_order_id].pop("stop_activation_known")
        await store.save(saved)
        broker = PaperBrokerProvider(
            settings,
            clock=fake_clock,
            quote_source=source,
            constraint_source=database_constraints,
            state_store=store,
        )
        await broker.restore()
    market = replace(market, ltp=Decimal(105))
    if case == "future":
        market = replace(market, observed_at=fake_clock.now() + timedelta(seconds=1))
    if case == "storage":

        async def failed_save(snapshot):
            raise OSError("isolated database failure")

        monkeypatch.setattr(store, "save", failed_save)
        with pytest.raises(OSError, match="isolated database failure"):
            await broker.settle_open_orders()
        assert broker.account.cash == 500000
        assert await broker.list_trades(ack.broker_order_id, Segment.FNO) == []
        assert (await broker.get_order(ack.broker_order_id, Segment.FNO)).raw[
            "stop_activation"
        ] is None
        return
    await broker.settle_open_orders()
    remote = await broker.get_order(ack.broker_order_id, Segment.FNO)
    if case in {"future", "latency", "legacy"}:
        assert remote.raw["stop_activation"] is None
        assert remote.status == (OrderStatus.REJECTED if case == "legacy" else OrderStatus.OPEN)
    if case == "modify":
        with pytest.raises(ValidationError, match="cancel and replace"):
            await broker.modify_order(
                ModifyRequest(
                    ack.broker_order_id,
                    Segment.FNO,
                    trigger_price=Decimal(106),
                )
            )
        with pytest.raises(ValidationError, match="cancel and replace"):
            await broker.modify_order(
                ModifyRequest(
                    ack.broker_order_id,
                    Segment.FNO,
                    order_type=OrderType.LIMIT,
                )
            )
    if case == "cancel":
        await broker.cancel_order(ack.broker_order_id, Segment.FNO)
    fake_clock.advance(timedelta(seconds=1))
    market = quote(fake_clock, asks=(("100", 100),))
    if case == "modify":
        await broker.modify_order(ModifyRequest(ack.broker_order_id, Segment.FNO, quantity=75))
    await broker.settle_open_orders()
    fills = await broker.list_trades(ack.broker_order_id, Segment.FNO)
    assert sum(fill.quantity for fill in fills) == (75 if case == "modify" else 0)
    if case == "cancel":
        assert (
            await broker.get_order(ack.broker_order_id, Segment.FNO)
        ).status == OrderStatus.CANCELLED


@pytest.mark.parametrize("corrupt", ["state", "crossing"])
async def test_corrupt_activation_evidence_fails_restore(
    db_engine, settings_env, fake_clock, corrupt
):
    await contract()
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    market = replace(quote(fake_clock, asks=(("107", 100),)), ltp=Decimal(105))

    async def source(reference):
        return market

    store = PaperStateStore()
    broker = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=store,
        fill_config=FillConfig(latency_ms=0),
    )
    ack = await broker.place_order(
        request(
            quantity=100,
            order_type=OrderType.STOP_LOSS,
            trigger_price=Decimal(105),
            price=Decimal(106),
        )
    )
    saved = broker.snapshot()
    order = saved["orders"][ack.broker_order_id]
    if corrupt == "state":
        order["stop_activation_known"] = "true"
    else:
        order["stop_activation"]["ltp"] = "100"
    await store.save(saved)
    restored = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        quote_source=source,
        constraint_source=database_constraints,
        state_store=store,
    )
    with pytest.raises(ValidationError if corrupt == "state" else SchemaError):
        await restored.restore()
