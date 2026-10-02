"""Groww endpoint modules against the contract fixtures.

Covers GRW-007…GRW-020, GRW-025, GRW-026.

These assert two things per endpoint: the request we build matches the documented
shape, and the response we parse produces the right typed model — including the
cases where a value is absent and must stay ``None``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.brokers.groww.capabilities import GROWW_CAPABILITIES, assert_supported
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.errors import DuplicateOrderReferenceError, GrowwNotFoundError
from app.brokers.groww.historical import (
    INTRADAY_HISTORY_DAYS,
    MAX_RANGE_DAYS,
    GrowwHistoricalApi,
    split_window,
)
from app.brokers.groww.margin import GrowwMarginApi
from app.brokers.groww.marketdata import MAX_BATCH_SYMBOLS, GrowwMarketDataApi
from app.brokers.groww.options import GrowwOptionsApi
from app.brokers.groww.orders import GrowwOrdersApi
from app.brokers.groww.portfolio import GrowwPortfolioApi
from app.brokers.models import ModifyRequest, OrderRequest
from app.core.clock import IST, FakeClock
from app.core.enums import (
    Exchange,
    GreekSource,
    MarginEstimateSource,
    OptionType,
    OrderStatus,
    OrderType,
    Product,
    Segment,
    TransactionType,
    Validity,
)
from app.core.errors import InvalidResponseError, ValidationError
from app.marketdata.models import InstrumentRef

from tests.integration.groww.conftest import MockGroww, load_fixture

pytestmark = pytest.mark.integration

CREATE = "/v1/order/create"
DETAIL = "/v1/order/detail/GMK39038RDT490CCVRO"
MODIFY = "/v1/order/modify"
CANCEL = "/v1/order/cancel"
LIST = "/v1/order/list"
TRADES = "/v1/order/trades/GMK39038RDT490CCVRO"
POSITIONS = "/v1/positions/user"
HOLDINGS = "/v1/holdings/user"
MARGIN = "/v1/margins/detail/user"
QUOTE = "/v1/live-data/quote"
LTP = "/v1/live-data/ltp"
OHLC = "/v1/live-data/ohlc"
CANDLES = "/v1/historical/candle/range"
CHAIN = "/v1/live-data/option-chain"
GREEKS = "/v1/live-data/greeks"


def _order_request(**overrides) -> OrderRequest:
    defaults = dict(
        trading_symbol="WIPRO",
        exchange=Exchange.NSE,
        segment=Segment.CASH,
        product=Product.CNC,
        order_type=OrderType.LIMIT,
        transaction_type=TransactionType.BUY,
        quantity=100,
        reference_id="ABC12345678",
        price=Decimal("250.50"),
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


# --- GRW-007 --------------------------------------------------------------


async def test_groww_place_order_payload(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(CREATE, "order_create_success.json")
    api = GrowwOrdersApi(groww_client)

    ack = await api.place(_order_request())

    body = mock_groww.body(CREATE)
    assert body == {
        "trading_symbol": "WIPRO",
        "quantity": 100,
        "validity": "DAY",
        "exchange": "NSE",
        "segment": "CASH",
        "product": "CNC",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
        "order_reference_id": "ABC12345678",
        "price": 250.5,
    }
    assert ack.broker_order_id == "GMK39038RDT490CCVRO"
    assert ack.status is OrderStatus.OPEN
    assert ack.reference_id == "ABC12345678"


async def test_algo_id_is_sent_only_when_configured(
    groww_client, mock_groww: MockGroww
) -> None:
    """CMP-002: the compliance tag travels with the order when it exists."""
    mock_groww.fixture(CREATE, "order_create_success.json")
    api = GrowwOrdersApi(groww_client)

    await api.place(_order_request(algo_id="ALGO-123"))
    assert mock_groww.body(CREATE)["algo_id"] == "ALGO-123"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"quantity": 0}, "positive"),
        ({"order_type": OrderType.LIMIT, "price": None}, "require a price"),
        ({"order_type": OrderType.MARKET}, "must not carry a price"),
        ({"order_type": OrderType.STOP_LOSS_MARKET, "price": None}, "trigger price"),
        ({"reference_id": "short"}, "characters"),
        ({"reference_id": "not valid!!!!!"}, "alphanumeric"),
    ],
)
async def test_invalid_orders_are_rejected_locally(
    groww_client, mock_groww: MockGroww, overrides: dict, fragment: str
) -> None:
    """An avoidable rejection must never reach the broker."""
    mock_groww.fixture(CREATE, "order_create_success.json")
    api = GrowwOrdersApi(groww_client)

    with pytest.raises(ValidationError) as excinfo:
        await api.place(_order_request(**overrides))
    assert fragment in str(excinfo.value)
    assert mock_groww.count(CREATE) == 0


async def test_duplicate_reference_surfaces_as_its_own_error(
    groww_client, mock_groww: MockGroww
) -> None:
    """GA007 is the idempotency signal, and must be distinguishable."""
    mock_groww.fixture(CREATE, "order_create_duplicate.json")
    api = GrowwOrdersApi(groww_client)

    with pytest.raises(DuplicateOrderReferenceError) as excinfo:
        await api.place(_order_request())
    assert excinfo.value.retryable is False
    assert mock_groww.count(CREATE) == 1


# --- GRW-008 / GRW-009 ----------------------------------------------------


async def test_groww_modify_order(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture("/v1/order/detail/GMK39038RDT490CCVRP", "order_detail_open.json")
    mock_groww.fixture(MODIFY, "order_create_success.json")
    api = GrowwOrdersApi(groww_client)

    await api.modify(
        ModifyRequest(
            broker_order_id="GMK39038RDT490CCVRP",
            segment=Segment.FNO,
            quantity=75,
            price=Decimal("118.00"),
        )
    )
    body = mock_groww.body(MODIFY)
    assert body["groww_order_id"] == "GMK39038RDT490CCVRP"
    assert body["segment"] == "FNO"
    assert body["quantity"] == 75
    assert body["price"] == 118.0


async def test_modify_of_a_terminal_order_is_refused_locally(
    groww_client, mock_groww: MockGroww
) -> None:
    mock_groww.fixture(DETAIL, "order_detail_filled.json")
    api = GrowwOrdersApi(groww_client)

    with pytest.raises(ValidationError) as excinfo:
        await api.modify(
            ModifyRequest(
                broker_order_id="GMK39038RDT490CCVRO",
                segment=Segment.CASH,
                quantity=50,
            )
        )
    assert "EXECUTED" in str(excinfo.value)
    assert mock_groww.count(MODIFY) == 0


async def test_groww_cancel_order(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(CANCEL, "order_cancelled.json")
    api = GrowwOrdersApi(groww_client)

    ack = await api.cancel("GMK39038RDT490CCVRO", Segment.CASH)
    assert ack.status is OrderStatus.CANCELLED
    assert mock_groww.body(CANCEL) == {
        "groww_order_id": "GMK39038RDT490CCVRO",
        "segment": "CASH",
    }


async def test_cancelling_an_already_cancelled_order_succeeds(
    groww_client, mock_groww: MockGroww
) -> None:
    """Emergency sweeps cancel repeatedly; that must not raise mid-sweep."""
    mock_groww.json(CANCEL, load_fixture("bad_request.json"))
    mock_groww.fixture(DETAIL, "order_cancelled.json")
    api = GrowwOrdersApi(groww_client)

    ack = await api.cancel("GMK39038RDT490CCVRO", Segment.CASH)
    assert ack.status is OrderStatus.CANCELLED
    assert ack.remark == "already cancelled"


# --- GRW-010 --------------------------------------------------------------


async def test_groww_order_status(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(DETAIL, "order_detail_filled.json")
    api = GrowwOrdersApi(groww_client)

    order = await api.get("GMK39038RDT490CCVRO", Segment.CASH)
    assert order.status is OrderStatus.EXECUTED
    assert order.quantity == 100
    assert order.filled_quantity == 100
    assert order.remaining_quantity == 0
    assert order.average_fill_price == Decimal("250.35")
    assert order.price == Decimal("250.5")
    assert order.trigger_price is None  # absent stays absent
    assert order.product is Product.CNC
    assert order.created_at is not None
    assert order.created_at.utcoffset() == timedelta(hours=5, minutes=30)


async def test_get_order_by_reference_returns_none_when_absent(
    groww_client, mock_groww: MockGroww
) -> None:
    """The signal that a timed-out submission never reached the exchange."""
    path = "/v1/order/status/reference/ABC12345678"
    mock_groww.json(path, load_fixture("order_not_found.json"), status=404)
    api = GrowwOrdersApi(groww_client)

    assert await api.get_by_reference("ABC12345678", Segment.CASH) is None


async def test_get_order_by_reference_returns_the_existing_order(
    groww_client, mock_groww: MockGroww
) -> None:
    path = "/v1/order/status/reference/ABC12345678"
    mock_groww.fixture(path, "order_detail_filled.json")
    api = GrowwOrdersApi(groww_client)

    order = await api.get_by_reference("ABC12345678", Segment.CASH)
    assert order is not None
    assert order.broker_order_id == "GMK39038RDT490CCVRO"
    assert order.reference_id == "ABC12345678"


async def test_unknown_order_id_raises_not_found(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.json(DETAIL, load_fixture("order_not_found.json"), status=404)
    api = GrowwOrdersApi(groww_client)
    with pytest.raises(GrowwNotFoundError):
        await api.get("GMK39038RDT490CCVRO", Segment.CASH)


# --- GRW-011 / GRW-012 ----------------------------------------------------


async def test_groww_order_list_pagination(groww_client, mock_groww: MockGroww) -> None:
    """A short page ends the loop; page size is clamped to the documented 100."""
    mock_groww.fixture(LIST, "order_list.json")
    api = GrowwOrdersApi(groww_client)

    orders = await api.list()
    assert len(orders) == 2
    assert {o.broker_order_id for o in orders} == {
        "GMK39038RDT490CCVRO",
        "GMK39038RDT490CCVRP",
    }
    assert mock_groww.count(LIST) == 1
    assert mock_groww.last(LIST).url.params["page_size"] == "100"


async def test_order_list_walks_full_pages(groww_client, mock_groww: MockGroww) -> None:
    full_page = {
        "status": "SUCCESS",
        "payload": {
            "order_list": [
                {
                    "groww_order_id": f"ORDER{index:04d}",
                    "trading_symbol": "WIPRO",
                    "order_status": "EXECUTED",
                    "quantity": 1,
                    "filled_quantity": 1,
                }
                for index in range(100)
            ]
        },
    }
    tail = {"status": "SUCCESS", "payload": {"order_list": []}}
    mock_groww.sequence(
        LIST,
        [httpx.Response(200, json=full_page), httpx.Response(200, json=tail)],
    )
    api = GrowwOrdersApi(groww_client)

    orders = await api.list()
    assert len(orders) == 100
    assert mock_groww.count(LIST) == 2


async def test_groww_trade_list(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(TRADES, "trades_two_fills.json")
    api = GrowwOrdersApi(groww_client)

    trades = await api.trades("GMK39038RDT490CCVRO", Segment.CASH)
    assert [t.quantity for t in trades] == [40, 60]
    assert trades[0].price == Decimal("250.3")
    assert trades[0].exchange_trade_id == "2026091712345"
    assert trades[0].isin == "INE075A01022"
    assert trades[0].executed_at is not None
    assert mock_groww.last(TRADES).url.params["page_size"] == "50"


# --- GRW-013 / GRW-014 ----------------------------------------------------


async def test_groww_positions_normalisation(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(POSITIONS, "positions.json")
    api = GrowwPortfolioApi(groww_client)

    positions = await api.positions()
    assert len(positions) == 2

    long_position = positions[0]
    assert long_position.net_quantity == 100
    assert long_position.average_price == Decimal("250.35")
    assert long_position.segment is Segment.CASH

    # A sold-only position must come back negative, or exposure is understated.
    short_position = positions[1]
    assert short_position.net_quantity == -75
    assert short_position.segment is Segment.FNO
    assert short_position.product is Product.NRML


async def test_groww_holdings(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(HOLDINGS, "holdings.json")
    api = GrowwPortfolioApi(groww_client)

    holdings = await api.holdings()
    assert len(holdings) == 2
    assert holdings[0].isin == "INE467B01029"
    assert holdings[0].quantity == 12
    # The unmapped holding is kept, not dropped: it is still real exposure.
    assert holdings[1].trading_symbol == "UNMAPPED"
    assert holdings[1].isin is None


# --- GRW-015 --------------------------------------------------------------


async def test_groww_margin(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(MARGIN, "margin.json")
    api = GrowwMarginApi(groww_client)

    margin = await api.available()
    assert margin.available_margin == Decimal("248500.5")
    assert margin.used_margin == Decimal("51499.5")
    assert margin.cash == Decimal("250000.0")
    assert margin.source is MarginEstimateSource.BROKER


async def test_margin_without_an_available_figure_is_an_error(
    groww_client, mock_groww: MockGroww
) -> None:
    """Silently defaulting available margin to zero would block all trading."""
    mock_groww.json(MARGIN, {"status": "SUCCESS", "payload": {"user_id": "X"}})
    api = GrowwMarginApi(groww_client)

    with pytest.raises(InvalidResponseError):
        await api.available()


# --- GRW-017 --------------------------------------------------------------


async def test_quote_parses_every_documented_field(
    groww_client, mock_groww: MockGroww
) -> None:
    mock_groww.fixture(QUOTE, "quote.json")
    api = GrowwMarketDataApi(groww_client)
    ref = InstrumentRef("RELIANCE", Exchange.NSE, Segment.CASH)

    quote = await api.quote(ref)
    assert quote.ltp == Decimal("1402.5")
    assert quote.open == Decimal("1396.0")
    assert quote.high == Decimal("1408.9")
    assert quote.volume == 4820115
    assert quote.upper_circuit == Decimal("1534.5")
    assert quote.lower_circuit == Decimal("1255.5")
    assert quote.week52_high == Decimal("1608.8")
    assert quote.best_bid == Decimal("1402.4")
    assert quote.best_ask == Decimal("1402.6")
    assert quote.spread == Decimal("0.2")
    assert not quote.is_crossed
    assert len(quote.bids) == 2
    assert quote.bids[0].quantity == 250
    # Cash instruments have no OI; it must stay None rather than becoming 0.
    assert quote.open_interest is None


async def test_groww_live_data_batching(groww_client, mock_groww: MockGroww) -> None:
    """A 130-symbol request becomes three calls, not 130."""
    captured: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["exchange_symbols"].split(",")
        captured.append(symbols)
        return httpx.Response(
            200,
            json={"status": "SUCCESS", "payload": {symbol: 100.0 for symbol in symbols}},
        )

    mock_groww.on(LTP, handler)
    api = GrowwMarketDataApi(groww_client)
    refs = [
        InstrumentRef(f"SYM{index:03d}", Exchange.NSE, Segment.CASH) for index in range(130)
    ]

    result = await api.ltp(refs)

    assert len(result) == 130
    assert mock_groww.count(LTP) == 3
    assert [len(batch) for batch in captured] == [MAX_BATCH_SYMBOLS, MAX_BATCH_SYMBOLS, 30]
    assert result["NSE_CASH_SYM000"].ltp == Decimal("100.0")


async def test_missing_batch_entries_are_reported_not_invented(
    groww_client, mock_groww: MockGroww, caplog
) -> None:
    mock_groww.fixture(LTP, "ltp_batch.json")
    api = GrowwMarketDataApi(groww_client)
    refs = [
        InstrumentRef("RELIANCE", Exchange.NSE, Segment.CASH),
        InstrumentRef("MISSING", Exchange.NSE, Segment.CASH),
    ]

    with caplog.at_level("WARNING"):
        result = await api.ltp(refs)

    assert "NSE_CASH_RELIANCE" in result
    assert "NSE_CASH_MISSING" not in result  # absent, never fabricated
    assert any("no data" in record.message for record in caplog.records)


async def test_ohlc_batch_parses_bars(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(OHLC, "ohlc_batch.json")
    api = GrowwMarketDataApi(groww_client)
    refs = [
        InstrumentRef("RELIANCE", Exchange.NSE, Segment.CASH),
        InstrumentRef("TCS", Exchange.NSE, Segment.CASH),
    ]

    result = await api.ohlc(refs)
    assert result["NSE_CASH_RELIANCE"].high == Decimal("1408.9")
    assert result["NSE_CASH_TCS"].previous_close == Decimal("3888.0")


# --- GRW-018 --------------------------------------------------------------


def test_documented_interval_limits() -> None:
    assert MAX_RANGE_DAYS[1] == 7
    assert MAX_RANGE_DAYS[5] == 15
    assert MAX_RANGE_DAYS[10] == 30
    assert MAX_RANGE_DAYS[60] == 150
    assert MAX_RANGE_DAYS[240] == 365
    assert MAX_RANGE_DAYS[1440] == 1080
    assert MAX_RANGE_DAYS[10080] is None
    assert INTRADAY_HISTORY_DAYS == 90


def test_groww_historical_windowing() -> None:
    """A 60-day request at 5-minute bars becomes compliant 15-day windows."""
    start = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    end = datetime(2026, 7, 31, 15, 30, tzinfo=IST)

    windows = split_window(start, end, 5)
    assert len(windows) == 5
    assert windows[0][0] == start
    assert windows[-1][1] == end

    # Contiguous and non-overlapping: no bar can be fetched twice or skipped.
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == previous_end + timedelta(seconds=1)
    for window_start, window_end in windows:
        assert (window_end - window_start) <= timedelta(days=15)


def test_window_splitting_edge_cases() -> None:
    start = datetime(2026, 6, 1, 9, 15, tzinfo=IST)

    # Within one window: a single request.
    assert len(split_window(start, start + timedelta(days=3), 5)) == 1
    # Weekly has no documented cap.
    assert len(split_window(start, start + timedelta(days=4000), 10080)) == 1

    with pytest.raises(ValidationError):
        split_window(start, start - timedelta(days=1), 5)
    with pytest.raises(ValidationError):
        split_window(start, start + timedelta(days=1), 3)


async def test_historical_fetch_stitches_windows(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock
) -> None:
    mock_groww.fixture(CANDLES, "historical_candles.json")
    api = GrowwHistoricalApi(groww_client, clock=fake_clock)
    ref = InstrumentRef("NIFTY", Exchange.NSE, Segment.FNO)

    bars = await api.candles(
        ref,
        5,
        datetime(2026, 1, 5, 9, 15, tzinfo=IST),
        datetime(2026, 1, 5, 9, 30, tzinfo=IST),
    )

    assert len(bars) == 3
    assert bars[0].open == Decimal("24500.0")
    assert bars[0].volume == 128400
    assert bars[0].open_interest == 9875200
    # Ascending, de-duplicated by timestamp.
    assert [bar.ts for bar in bars] == sorted(bar.ts for bar in bars)


async def test_history_beyond_the_three_month_wall_is_warned_about(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock, caplog
) -> None:
    """A truncated series must not look like a complete one (BT-014)."""
    mock_groww.fixture(CANDLES, "historical_candles.json")
    api = GrowwHistoricalApi(groww_client, clock=fake_clock)
    ref = InstrumentRef("NIFTY", Exchange.NSE, Segment.FNO)

    with caplog.at_level("WARNING"):
        await api.candles(
            ref,
            5,
            fake_clock.now() - timedelta(days=200),
            fake_clock.now(),
        )

    assert any("intraday limit" in record.message for record in caplog.records)


async def test_inconsistent_candles_are_rejected(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock
) -> None:
    mock_groww.fixture(CANDLES, "historical_inconsistent.json")
    api = GrowwHistoricalApi(groww_client, clock=fake_clock)
    ref = InstrumentRef("NIFTY", Exchange.NSE, Segment.FNO)

    with pytest.raises(InvalidResponseError):
        await api.candles(
            ref, 5, fake_clock.now() - timedelta(days=1), fake_clock.now()
        )


def test_daily_history_has_no_intraday_wall(groww_client, fake_clock: FakeClock) -> None:
    api = GrowwHistoricalApi(groww_client, clock=fake_clock)
    assert api.earliest_available(1440) is None
    assert api.earliest_available(10080) is None
    assert api.earliest_available(5) is not None


# --- GRW-019 / GRW-020 ----------------------------------------------------


async def test_groww_option_chain(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock
) -> None:
    mock_groww.fixture(CHAIN, "option_chain.json")
    api = GrowwOptionsApi(groww_client, clock=fake_clock)

    chain = await api.chain("NIFTY", date(2026, 9, 24))

    assert chain.underlying == "NIFTY"
    assert chain.spot == Decimal("24512.35")
    assert len(chain.strikes) == 2
    assert [s.strike for s in chain.strikes] == [Decimal("24400"), Decimal("24500")]
    assert chain.atm_strike() == Decimal("24500")

    first = chain.strikes[0]
    assert first.call is not None and first.put is not None
    assert first.call.option_type is OptionType.CE
    assert first.call.last_price if False else first.call.ltp == Decimal("168.4")
    assert first.call.open_interest == 1245600
    assert first.call.open_interest_change == -42300
    assert first.put.open_interest == 2984100


async def test_groww_greeks_parsing(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock
) -> None:
    mock_groww.fixture(CHAIN, "option_chain.json")
    api = GrowwOptionsApi(groww_client, clock=fake_clock)

    chain = await api.chain("NIFTY", date(2026, 9, 24))
    call = chain.strikes[0].call
    assert call is not None and call.greeks is not None
    assert call.greeks.delta == Decimal("0.62")
    assert call.greeks.theta == Decimal("-12.8")
    assert call.greeks.implied_volatility == Decimal("12.6")
    assert call.greeks.source is GreekSource.BROKER

    # An empty greeks object means "not supplied", not "all zero".
    second_call = chain.strikes[1].call
    assert second_call is not None
    assert second_call.greeks is None
    # A leg with no greeks key at all is equally absent.
    assert chain.strikes[1].put is not None
    assert chain.strikes[1].put.greeks is None


async def test_greeks_endpoint_returns_none_when_unsupplied(
    groww_client, mock_groww: MockGroww, fake_clock: FakeClock
) -> None:
    mock_groww.json(GREEKS, {"status": "SUCCESS", "payload": {}})
    api = GrowwOptionsApi(groww_client, clock=fake_clock)
    assert await api.greeks("NIFTY", "NIFTY26SEP24500CE") is None


# --- GRW-025 --------------------------------------------------------------


def test_broker_capabilities() -> None:
    caps = GROWW_CAPABILITIES
    assert caps.supported_validities == frozenset({Validity.DAY})
    assert not caps.supports_gtt
    assert not caps.supports_bracket_orders
    assert caps.max_batch_quote_symbols == 50
    assert caps.max_feed_subscriptions == 1000
    assert caps.max_orders_per_second == 10
    assert caps.max_orders_per_minute == 250
    assert Segment.FNO in caps.supported_segments
    assert Product.MIS in caps.supported_products


def test_unsupported_requests_fail_locally() -> None:
    # BSE is not confirmed on the order path.
    with pytest.raises(ValidationError) as excinfo:
        assert_supported(
            exchange=Exchange.BSE,
            segment=Segment.CASH,
            product=Product.CNC,
            order_type=OrderType.LIMIT,
            validity=Validity.DAY,
        )
    assert "exchange BSE" in str(excinfo.value)

    # And the supported combination passes.
    assert_supported(
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        product=Product.NRML,
        order_type=OrderType.STOP_LOSS_MARKET,
        validity=Validity.DAY,
    )


# --- GRW-026 --------------------------------------------------------------


def test_groww_contract_fixtures() -> None:
    """Every fixture must parse as the envelope the documentation describes."""
    from tests.integration.groww.conftest import FIXTURE_ROOT

    files = sorted(FIXTURE_ROOT.glob("*.json"))
    assert len(files) >= 20

    successes = 0
    failures = 0
    for path in files:
        body = load_fixture(path.name)
        assert "status" in body, path.name
        if body["status"] == "SUCCESS":
            assert "payload" in body, path.name
            successes += 1
        else:
            assert body["status"] == "FAILURE", path.name
            assert set(body["error"]) >= {"code", "message"}, path.name
            failures += 1

    # Both paths are covered, as GRW-026 requires.
    assert successes >= 10
    assert failures >= 4
