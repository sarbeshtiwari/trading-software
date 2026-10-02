"""Paper broker: account, fills, persistence, idempotency.

Covers PAPER-001, PAPER-003, PAPER-004, PAPER-005, PAPER-007, EXEC-014,
and the position arithmetic the portfolio layer will later depend on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest

from app.brokers.base import BrokerProvider
from app.brokers.paper.account import MarginModel, PaperAccount
from app.brokers.paper.engine import FillConfig, FillEngine
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import InMemoryPaperStateStore, PaperStateStore
from app.brokers.models import ModifyRequest, OrderRequest
from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import (
    Exchange,
    OrderStatus,
    OrderType,
    Product,
    Segment,
    TransactionType,
    Validity,
)
from app.core.errors import ValidationError
from app.marketdata.models import DepthLevel, InstrumentRef, Quote

pytestmark = pytest.mark.integration


def _quote(
    ltp: str,
    *,
    bids: tuple[tuple[str, int], ...] = (),
    asks: tuple[tuple[str, int], ...] = (),
    symbol: str = "WIPRO",
    segment: Segment = Segment.CASH,
    clock=None,
) -> Quote:
    from app.core.clock import get_clock

    return Quote(
        instrument=InstrumentRef(symbol, Exchange.NSE, segment),
        ltp=Decimal(ltp),
        observed_at=(clock or get_clock()).now(),
        data_origin=DataOrigin.LIVE,
        bids=tuple(DepthLevel(price=Decimal(p), quantity=q) for p, q in bids),
        asks=tuple(DepthLevel(price=Decimal(p), quantity=q) for p, q in asks),
    )


def _request(**overrides) -> OrderRequest:
    defaults = dict(
        trading_symbol="WIPRO",
        exchange=Exchange.NSE,
        segment=Segment.CASH,
        product=Product.MIS,
        order_type=OrderType.MARKET,
        transaction_type=TransactionType.BUY,
        quantity=100,
        reference_id="PAPER000001",
        validity=Validity.DAY,
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


@pytest.fixture
def paper(settings_env, fake_clock):
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    provider = PaperBrokerProvider(
        settings,
        clock=fake_clock,
        fill_config=FillConfig(seed=7, latency_ms=0),
        state_store=InMemoryPaperStateStore(),
    )
    return provider


# --- PAPER-001 ------------------------------------------------------------


def test_paper_provider_parity() -> None:
    """The paper broker implements every method of the interface."""
    assert not getattr(PaperBrokerProvider, "__abstractmethods__", frozenset())
    for name in BrokerProvider.__abstractmethods__:
        assert hasattr(PaperBrokerProvider, name), name


async def test_paper_is_always_simulated(paper) -> None:
    assert paper.execution_realism is ExecutionRealism.SIMULATED
    profile = await paper.get_profile()
    assert profile.raw["simulated"] is True


def test_paper_mirrors_the_real_broker_constraints(paper) -> None:
    """A strategy must not be able to rely on something Groww would refuse."""
    from app.brokers.groww.capabilities import GROWW_CAPABILITIES

    caps = paper.capabilities
    assert caps.supported_validities == GROWW_CAPABILITIES.supported_validities
    assert caps.supported_order_types == GROWW_CAPABILITIES.supported_order_types
    assert caps.supports_gtt is False
    assert caps.max_orders_per_second == GROWW_CAPABILITIES.max_orders_per_second


async def test_unsupported_orders_are_refused(paper) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("250")))
    with pytest.raises(ValidationError):
        await paper.place_order(_request(exchange=Exchange.BSE))


async def _resolve(quote: Optional[Quote]) -> Optional[Quote]:
    return quote


# --- PAPER-003: account and margin ---------------------------------------


def test_paper_account_margin() -> None:
    account = PaperAccount(starting_capital=Decimal("100000"))
    assert account.cash == Decimal("100000")
    assert account.available_margin == Decimal("100000.00")

    # MIS at 20% of notional: 100 x 250 = 25000 notional -> 5000 required.
    assert account.requirement_for(100, Decimal("250"), Product.MIS, Segment.CASH) == Decimal(
        "5000.00"
    )
    # CNC requires the full value.
    assert account.requirement_for(100, Decimal("250"), Product.CNC, Segment.CASH) == Decimal(
        "25000.00"
    )
    # F&O carries the extra safety multiplier on top.
    assert account.requirement_for(
        75, Decimal("24500"), Product.NRML, Segment.FNO
    ) == Decimal("441000.00")

    assert account.can_afford(100, Decimal("250"), Product.MIS, Segment.CASH)
    assert not account.can_afford(1000, Decimal("250"), Product.CNC, Segment.CASH)


def test_margin_is_reserved_and_released_across_a_trade() -> None:
    account = PaperAccount(starting_capital=Decimal("100000"))

    account.apply_fill(
        trading_symbol="WIPRO",
        segment=Segment.CASH,
        product=Product.MIS,
        transaction_type=TransactionType.BUY,
        quantity=100,
        price=Decimal("250"),
    )
    assert account.used_margin == Decimal("5000.00")
    assert account.available_margin == Decimal("95000.00")

    account.apply_fill(
        trading_symbol="WIPRO",
        segment=Segment.CASH,
        product=Product.MIS,
        transaction_type=TransactionType.SELL,
        quantity=100,
        price=Decimal("255"),
    )
    assert account.used_margin == Decimal("0.00")
    assert account.realised_pnl == Decimal("500.00")
    assert account.cash == Decimal("100500.00")


def test_average_price_arithmetic_across_scale_in_and_exit() -> None:
    account = PaperAccount(starting_capital=Decimal("1000000"))
    common = dict(trading_symbol="INFY", segment=Segment.CASH, product=Product.MIS)

    account.apply_fill(**common, transaction_type=TransactionType.BUY, quantity=100,
                       price=Decimal("1500"))
    account.apply_fill(**common, transaction_type=TransactionType.BUY, quantity=100,
                       price=Decimal("1520"))
    position = account.positions["CASH:MIS:INFY"]
    assert position.net_quantity == 200
    assert position.average_price == Decimal("1510.00")

    # Partial exit realises on the closed portion only.
    account.apply_fill(**common, transaction_type=TransactionType.SELL, quantity=50,
                       price=Decimal("1530"))
    assert position.net_quantity == 150
    assert position.average_price == Decimal(227000) / 150
    assert position.realised_pnl == Decimal("1500.00")


def test_short_position_and_reversal_arithmetic() -> None:
    account = PaperAccount(starting_capital=Decimal("1000000"))
    common = dict(trading_symbol="SBIN", segment=Segment.CASH, product=Product.MIS)

    account.apply_fill(**common, transaction_type=TransactionType.SELL, quantity=100,
                       price=Decimal("800"))
    position = account.positions["CASH:MIS:SBIN"]
    assert position.net_quantity == -100
    assert position.average_price == Decimal("800.00")

    # Covering below the short price is a profit.
    account.apply_fill(**common, transaction_type=TransactionType.BUY, quantity=50,
                       price=Decimal("790"))
    assert position.net_quantity == -50
    assert position.realised_pnl == Decimal("500.00")

    # Reversing through zero: the remainder opens long at the fill price.
    account.apply_fill(**common, transaction_type=TransactionType.BUY, quantity=80,
                       price=Decimal("795"))
    assert position.net_quantity == 30
    assert position.average_price == Decimal("795.00")


def test_unrealised_pnl_and_equity_track_marks() -> None:
    account = PaperAccount(starting_capital=Decimal("100000"))
    account.apply_fill(
        trading_symbol="WIPRO",
        segment=Segment.CASH,
        product=Product.MIS,
        transaction_type=TransactionType.BUY,
        quantity=100,
        price=Decimal("250"),
    )
    account.mark("WIPRO", Segment.CASH, Product.MIS, Decimal("262"))
    assert account.unrealised_pnl() == Decimal("1200.00")
    assert account.equity == Decimal("101200.00")


def test_margin_model_is_configurable() -> None:
    account = PaperAccount(
        starting_capital=Decimal("100000"),
        margin_model=MarginModel(mis=Decimal("0.50")),
    )
    assert account.requirement_for(100, Decimal("100"), Product.MIS, Segment.CASH) == Decimal(
        "5000.00"
    )


# --- PAPER-004: fills against the depth book ------------------------------


def test_market_order_walks_the_depth_book() -> None:
    engine = FillEngine(FillConfig(seed=1))
    quote = _quote(
        "250",
        asks=(("250.10", 40), ("250.25", 30), ("250.50", 100)),
        bids=(("249.90", 50),),
    )

    outcome = engine.simulate(
        order_type=OrderType.MARKET,
        transaction_type=TransactionType.BUY,
        quantity=100,
        limit_price=None,
        trigger_price=None,
        quote=quote,
    )

    assert outcome.status is OrderStatus.EXECUTED
    # Consumed three levels at progressively worse prices.
    assert [(f.quantity, str(f.price)) for f in outcome.fills] == [
        (40, "250.10"),
        (30, "250.25"),
        (30, "250.50"),
    ]
    # (40*250.10 + 30*250.25 + 30*250.50) / 100 = 250.265 -> 250.27 (HALF_UP)
    assert outcome.average_price == Decimal("250.27")


def test_thin_book_fills_partially_rather_than_inventing_liquidity() -> None:
    engine = FillEngine(FillConfig(seed=1))
    quote = _quote("250", asks=(("250.10", 25),))

    outcome = engine.simulate(
        order_type=OrderType.MARKET,
        transaction_type=TransactionType.BUY,
        quantity=100,
        limit_price=None,
        trigger_price=None,
        quote=quote,
    )
    assert outcome.status is OrderStatus.PARTIALLY_FILLED
    assert outcome.filled_quantity == 25


def test_slippage_always_works_against_the_order() -> None:
    engine = FillEngine(FillConfig(slippage_bps=Decimal("100"), use_depth=False))
    quote = _quote("100")

    buy = engine.simulate(
        order_type=OrderType.MARKET, transaction_type=TransactionType.BUY,
        quantity=10, limit_price=None, trigger_price=None, quote=quote,
    )
    sell = engine.simulate(
        order_type=OrderType.MARKET, transaction_type=TransactionType.SELL,
        quantity=10, limit_price=None, trigger_price=None, quote=quote,
    )
    assert buy.fills[0].price == Decimal("101.00")
    assert sell.fills[0].price == Decimal("99.00")


def test_limit_order_rests_until_marketable() -> None:
    engine = FillEngine(FillConfig(seed=1))
    quote = _quote("250", asks=(("250.10", 100),))

    resting = engine.simulate(
        order_type=OrderType.LIMIT, transaction_type=TransactionType.BUY,
        quantity=100, limit_price=Decimal("249.00"), trigger_price=None, quote=quote,
    )
    assert resting.status is OrderStatus.OPEN
    assert not resting.fills

    marketable = engine.simulate(
        order_type=OrderType.LIMIT, transaction_type=TransactionType.BUY,
        quantity=100, limit_price=Decimal("251.00"), trigger_price=None, quote=quote,
    )
    assert marketable.status is OrderStatus.EXECUTED
    assert marketable.fills[0].price == Decimal("250.10")


def test_stop_orders_wait_for_their_trigger() -> None:
    engine = FillEngine(FillConfig(seed=1, use_depth=False))

    untriggered = engine.simulate(
        order_type=OrderType.STOP_LOSS_MARKET, transaction_type=TransactionType.SELL,
        quantity=100, limit_price=None, trigger_price=Decimal("240"),
        quote=_quote("250"),
    )
    assert untriggered.status is OrderStatus.OPEN
    assert untriggered.reason == "trigger not reached"

    triggered = engine.simulate(
        order_type=OrderType.STOP_LOSS_MARKET, transaction_type=TransactionType.SELL,
        quantity=100, limit_price=None, trigger_price=Decimal("240"),
        quote=_quote("239.50"),
    )
    assert triggered.status is OrderStatus.EXECUTED


@pytest.mark.safety
def test_no_market_data_means_no_fill() -> None:
    """A price that does not exist must never become a fill."""
    engine = FillEngine()
    outcome = engine.simulate(
        order_type=OrderType.MARKET, transaction_type=TransactionType.BUY,
        quantity=100, limit_price=None, trigger_price=None, quote=None,
    )
    assert outcome.status is OrderStatus.OPEN
    assert not outcome.fills
    assert "no market data" in (outcome.reason or "")


def test_fill_engine_is_deterministic() -> None:
    config = FillConfig(seed=99, partial_fill_probability=0.5, use_depth=False)
    quote = _quote("250")

    def run() -> list[tuple[int, str]]:
        engine = FillEngine(config)
        results = []
        for _ in range(10):
            outcome = engine.simulate(
                order_type=OrderType.MARKET, transaction_type=TransactionType.BUY,
                quantity=100, limit_price=None, trigger_price=None, quote=quote,
            )
            results.append((outcome.filled_quantity, str(outcome.average_price)))
        return results

    assert run() == run()


# --- Provider behaviour ---------------------------------------------------


async def test_paper_fill_simulation(paper, fake_clock) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 200),))))

    ack = await paper.place_order(_request())
    assert ack.status is OrderStatus.EXECUTED

    order = await paper.get_order(ack.broker_order_id, Segment.CASH)
    assert order.filled_quantity == 100
    assert order.average_fill_price == Decimal("250.10")
    assert order.raw["simulated"] is True

    trades = await paper.list_trades(ack.broker_order_id, Segment.CASH)
    assert len(trades) == 1
    assert trades[0].raw["simulated"] is True

    positions = await paper.get_positions()
    assert positions[0].net_quantity == 100
    assert positions[0].raw["simulated"] is True


@pytest.mark.safety
async def test_duplicate_reference_never_creates_a_second_order(paper) -> None:
    """The same intent submitted twice must produce one position, not two."""
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 500),))))

    first = await paper.place_order(_request())
    second = await paper.place_order(_request())

    assert second.broker_order_id == first.broker_order_id
    assert "duplicate" in (second.remark or "")
    orders = await paper.list_orders()
    assert len(orders) == 1
    positions = await paper.get_positions()
    assert positions[0].net_quantity == 100


async def test_insufficient_margin_is_rejected(paper) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("25000")))
    ack = await paper.place_order(
        _request(product=Product.CNC, quantity=100, trading_symbol="MRF")
    )
    assert ack.status is OrderStatus.REJECTED
    assert "insufficient margin" in (ack.remark or "")
    assert not (await paper.get_positions())


async def test_resting_orders_fill_when_the_market_comes_to_them(paper) -> None:
    """settle_open_orders is what makes a stop actually trigger in paper."""
    market = {"quote": _quote("250", bids=(("249.90", 500),))}
    paper.set_quote_source(lambda ref: _resolve(market["quote"]))

    ack = await paper.place_order(
        _request(
            order_type=OrderType.STOP_LOSS_MARKET,
            transaction_type=TransactionType.SELL,
            trigger_price=Decimal("245"),
        )
    )
    assert ack.status is OrderStatus.OPEN

    # Price falls through the trigger.
    market["quote"] = _quote("244.50", bids=(("244.40", 500),))
    changed = await paper.settle_open_orders()

    assert len(changed) == 1
    assert changed[0].status is OrderStatus.EXECUTED
    positions = await paper.get_positions()
    assert positions[0].net_quantity == -100


async def test_cancel_is_idempotent(paper) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 500),))))
    ack = await paper.place_order(
        _request(order_type=OrderType.LIMIT, price=Decimal("240"))
    )
    assert ack.status is OrderStatus.OPEN

    first = await paper.cancel_order(ack.broker_order_id, Segment.CASH)
    second = await paper.cancel_order(ack.broker_order_id, Segment.CASH)
    assert first.status is OrderStatus.CANCELLED
    assert second.status is OrderStatus.CANCELLED
    assert second.remark == "already cancelled"


async def test_modify_reprices_a_resting_order(paper) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 500),))))
    ack = await paper.place_order(
        _request(order_type=OrderType.LIMIT, price=Decimal("240"))
    )
    assert ack.status is OrderStatus.OPEN

    modified = await paper.modify_order(
        ModifyRequest(
            broker_order_id=ack.broker_order_id,
            segment=Segment.CASH,
            price=Decimal("251"),
        )
    )
    assert modified.status is OrderStatus.EXECUTED


async def test_margin_reports_as_an_estimate(paper) -> None:
    from app.core.enums import MarginEstimateSource

    margin = await paper.get_margin()
    assert margin.source is MarginEstimateSource.ESTIMATED
    assert margin.available_margin == Decimal("500000.00")


async def test_get_order_by_reference_finds_the_order(paper) -> None:
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 500),))))
    ack = await paper.place_order(_request())

    found = await paper.get_order_by_reference("PAPER000001", Segment.CASH)
    assert found is not None
    assert found.broker_order_id == ack.broker_order_id
    assert await paper.get_order_by_reference("NOSUCHREF01", Segment.CASH) is None


# --- PAPER-005: persistence ----------------------------------------------


async def test_paper_state_persistence(settings_env, fake_clock, db_engine) -> None:
    """Positions and orders must survive a restart, as a real broker would."""
    settings = settings_env(STARTING_CAPITAL="500000", TRADING_MODE="PAPER")
    store = PaperStateStore()

    first = PaperBrokerProvider(
        settings, clock=fake_clock, fill_config=FillConfig(seed=3, latency_ms=0), state_store=store
    )
    first.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250.10", 500),))))
    ack = await first.place_order(_request())
    assert ack.status is OrderStatus.EXECUTED
    await first.close()

    # A fresh process, same store.
    second = PaperBrokerProvider(
        settings, clock=fake_clock, fill_config=FillConfig(seed=3, latency_ms=0), state_store=store
    )
    await second.restore()

    positions = await second.get_positions()
    assert positions[0].net_quantity == 100
    assert positions[0].average_price == Decimal("250.10")

    restored_order = await second.get_order_by_reference("PAPER000001", Segment.CASH)
    assert restored_order is not None
    assert restored_order.status is OrderStatus.EXECUTED
    assert restored_order.created_at == fake_clock.now()
    assert len(await second.list_trades(ack.broker_order_id, Segment.CASH)) == 1
    assert first._engine._rng.getstate() == second._engine._rng.getstate()
    # And the idempotency index survived too.
    duplicate = await second.place_order(_request())
    assert duplicate.broker_order_id == ack.broker_order_id


async def test_reducing_exit_is_not_rejected_for_new_entry_margin(paper):
    paper.set_quote_source(lambda ref: _resolve(_quote("250", asks=(("250", 500),), bids=(("250", 500),))))
    entry = await paper.place_order(_request())
    assert entry.status == OrderStatus.EXECUTED
    paper.account.cash = Decimal(0)
    exit_order = await paper.place_order(_request(
        transaction_type=TransactionType.SELL, reference_id="PAPER-EXIT-ONLY",
    ))
    assert exit_order.status == OrderStatus.EXECUTED
    assert (await paper.get_positions())[0].net_quantity == 0
