"""Provider interfaces and selection.

Covers ARCH-004, ARCH-005, ARCH-006, GRW-027.

The concrete Groww and paper providers arrive in P1. What is asserted here is the
*contract and the selection logic*: that the interfaces are fully abstract, that
an implementation must supply every method, and that configuration alone decides
which provider the engine gets.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Sequence

import pytest

from app.brokers.base import BrokerProvider
from app.brokers.factory import (
    available_broker_providers,
    create_broker_provider,
    register_broker_provider,
    resolve_broker_provider_name,
    unregister_broker_provider,
)
from app.brokers.models import (
    BrokerCapabilities,
    BrokerHolding,
    BrokerOrder,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    MarginInfo,
    ModifyRequest,
    OrderAck,
    OrderRequest,
)
from app.config import BrokerProviderName, Settings
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
from app.core.errors import ConfigurationError
from app.marketdata.base import MarketDataProvider
from app.marketdata.factory import (
    MarketDataProviderName,
    create_market_data_provider,
    register_market_data_provider,
    unregister_market_data_provider,
)
from app.marketdata.models import (
    Bar,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    OptionChain,
    Quote,
)
from app.modes import TradingMode

pytestmark = pytest.mark.unit


# --- Test doubles ---------------------------------------------------------


class _StubBroker(BrokerProvider):
    """A complete implementation used to prove the interface is satisfiable."""

    name = "stub"

    def __init__(self, realism: ExecutionRealism = ExecutionRealism.SIMULATED) -> None:
        self._realism = realism

    @property
    def execution_realism(self) -> ExecutionRealism:
        return self._realism

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="stub",
            supported_exchanges=frozenset({Exchange.NSE}),
            supported_segments=frozenset({Segment.CASH, Segment.FNO}),
            supported_products=frozenset({Product.CNC, Product.MIS, Product.NRML}),
            supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
            supported_validities=frozenset({Validity.DAY}),
        )

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> bool:
        return True

    async def get_profile(self) -> BrokerProfile:
        return BrokerProfile(account_id="stub-account")

    async def place_order(self, request: OrderRequest) -> OrderAck:
        return OrderAck(
            broker_order_id="stub-1", reference_id=request.reference_id,
            status=OrderStatus.OPEN,
        )

    async def modify_order(self, request: ModifyRequest) -> OrderAck:
        return OrderAck(
            broker_order_id=request.broker_order_id, reference_id=None,
            status=OrderStatus.OPEN,
        )

    async def cancel_order(self, broker_order_id: str, segment: Segment) -> OrderAck:
        return OrderAck(
            broker_order_id=broker_order_id, reference_id=None,
            status=OrderStatus.CANCELLED,
        )

    async def get_order(self, broker_order_id: str, segment: Segment) -> BrokerOrder:
        raise NotImplementedError

    async def get_order_by_reference(
        self, reference_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        return None

    async def list_orders(self, segment: Optional[Segment] = None) -> Sequence[BrokerOrder]:
        return ()

    async def list_trades(self, broker_order_id: str, segment: Segment) -> Sequence[BrokerTrade]:
        return ()

    async def get_positions(self) -> Sequence[BrokerPosition]:
        return ()

    async def get_holdings(self) -> Sequence[BrokerHolding]:
        return ()

    async def get_margin(self) -> MarginInfo:
        return MarginInfo(available_margin=Decimal("0"))


class _StubMarketData(MarketDataProvider):
    name = "stub"

    def __init__(self, origin: DataOrigin = DataOrigin.LIVE) -> None:
        self._origin = origin

    @property
    def data_origin(self) -> DataOrigin:
        return self._origin

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def get_quote(self, instrument: InstrumentRef) -> Quote:
        raise NotImplementedError

    async def get_ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        return {}

    async def get_ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        return {}

    async def get_index_value(self, name: str) -> IndexValue:
        raise NotImplementedError

    async def get_candles(
        self, instrument: InstrumentRef, interval_minutes: int,
        start: datetime, end: datetime,
    ) -> Sequence[Bar]:
        return ()

    async def get_option_chain(
        self, underlying: str, expiry: Optional[date] = None
    ) -> OptionChain:
        raise NotImplementedError

    async def subscribe(self, instruments: Sequence[InstrumentRef]) -> None: ...

    async def unsubscribe(self, instruments: Sequence[InstrumentRef]) -> None: ...

    def on_tick(self, callback) -> None: ...  # type: ignore[no-untyped-def]

    async def last_update_at(self, instrument: InstrumentRef) -> Optional[datetime]:
        return None


# --- ARCH-004 / ARCH-005 --------------------------------------------------


def _abstract_members(cls: type) -> set[str]:
    return set(getattr(cls, "__abstractmethods__", frozenset()))


def test_broker_interface_parity() -> None:
    """Every method the engine may call is abstract, and a stub can satisfy it."""
    expected = {
        "execution_realism",
        "capabilities",
        "connect",
        "close",
        "ping",
        "get_profile",
        "place_order",
        "modify_order",
        "cancel_order",
        "get_order",
        "get_order_by_reference",
        "list_orders",
        "list_trades",
        "get_positions",
        "get_holdings",
        "get_margin",
    }
    assert _abstract_members(BrokerProvider) == expected

    # An incomplete implementation must be impossible to instantiate.
    with pytest.raises(TypeError):
        BrokerProvider()  # type: ignore[abstract]

    broker = _StubBroker()
    assert not _abstract_members(type(broker))
    for name in expected:
        assert hasattr(broker, name)


def test_marketdata_interface_parity() -> None:
    expected = {
        "data_origin",
        "connect",
        "close",
        "get_quote",
        "get_ltp",
        "get_ohlc",
        "get_index_value",
        "get_candles",
        "get_option_chain",
        "subscribe",
        "unsubscribe",
        "on_tick",
        "last_update_at",
    }
    assert _abstract_members(MarketDataProvider) == expected

    with pytest.raises(TypeError):
        MarketDataProvider()  # type: ignore[abstract]

    provider = _StubMarketData()
    assert not _abstract_members(type(provider))


def test_batch_methods_take_sequences() -> None:
    """Groww batches LTP/OHLC at 50 per call; the interface must allow batching."""
    for method in (MarketDataProvider.get_ltp, MarketDataProvider.get_ohlc):
        signature = inspect.signature(method)
        assert "instruments" in signature.parameters


# --- ARCH-006 / GRW-027 ---------------------------------------------------


@pytest.fixture
def stub_registry():
    register_broker_provider(BrokerProviderName.PAPER, lambda _s: _StubBroker())
    register_broker_provider(
        BrokerProviderName.GROWW, lambda _s: _StubBroker(ExecutionRealism.REAL)
    )
    yield
    unregister_broker_provider(BrokerProviderName.PAPER)
    unregister_broker_provider(BrokerProviderName.GROWW)


def test_provider_factory_by_mode(settings_env, stub_registry) -> None:
    paper = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="paper")
    assert resolve_broker_provider_name(paper) is BrokerProviderName.PAPER
    assert create_broker_provider(paper).execution_realism is ExecutionRealism.SIMULATED

    supervised = settings_env(TRADING_MODE="SUPERVISED", BROKER_PROVIDER="groww")
    assert resolve_broker_provider_name(supervised) is BrokerProviderName.GROWW
    assert create_broker_provider(supervised).execution_realism is ExecutionRealism.REAL

    live = settings_env(TRADING_MODE="LIVE", BROKER_PROVIDER="groww")
    assert resolve_broker_provider_name(live) is BrokerProviderName.GROWW


def test_paper_mode_never_executes_through_groww(settings_env, stub_registry) -> None:
    """In PAPER, Groww may be a data source but never an execution venue."""
    settings = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="groww")
    assert resolve_broker_provider_name(settings) is BrokerProviderName.PAPER
    assert create_broker_provider(settings).execution_realism is ExecutionRealism.SIMULATED


def test_broker_switch_by_config(settings_env, stub_registry) -> None:
    """One environment variable moves the engine from paper to real Groww."""
    settings = settings_env(TRADING_MODE="SUPERVISED", BROKER_PROVIDER="groww")
    assert create_broker_provider(settings).execution_realism is ExecutionRealism.REAL

    settings = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="paper")
    assert create_broker_provider(settings).execution_realism is ExecutionRealism.SIMULATED


def test_unregistered_provider_fails_loudly(settings_env) -> None:
    settings = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="paper")
    assert BrokerProviderName.PAPER not in available_broker_providers()
    with pytest.raises(ConfigurationError) as excinfo:
        create_broker_provider(settings)
    assert "not registered" in str(excinfo.value)


def test_live_mode_cannot_resolve_to_paper_broker() -> None:
    """Defence in depth: settings reject it, and so does the factory."""
    settings = Settings.model_construct(
        trading_mode=TradingMode.LIVE, broker_provider=BrokerProviderName.PAPER
    )
    with pytest.raises(ConfigurationError):
        resolve_broker_provider_name(settings)


# --- Market-data selection ------------------------------------------------


@pytest.fixture
def stub_market_registry():
    register_market_data_provider(MarketDataProviderName.LIVE, lambda _s: _StubMarketData())
    register_market_data_provider(
        MarketDataProviderName.REPLAY, lambda _s: _StubMarketData(DataOrigin.REPLAY)
    )
    yield
    unregister_market_data_provider(MarketDataProviderName.LIVE)
    unregister_market_data_provider(MarketDataProviderName.REPLAY)


def test_paper_uses_live_data(settings_env, stub_market_registry) -> None:
    """PAPER-002: paper trading runs on real market data."""
    settings = settings_env(TRADING_MODE="PAPER")
    provider = create_market_data_provider(settings, name=MarketDataProviderName.LIVE)
    assert provider.data_origin is DataOrigin.LIVE


@pytest.mark.safety
def test_replay_data_refused_when_a_real_broker_is_reachable(
    settings_env, stub_market_registry
) -> None:
    settings = settings_env(TRADING_MODE="LIVE", BROKER_PROVIDER="groww")
    with pytest.raises(ConfigurationError):
        create_market_data_provider(settings, name=MarketDataProviderName.REPLAY)

    settings = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="paper")
    provider = create_market_data_provider(settings, name=MarketDataProviderName.REPLAY)
    assert provider.data_origin is DataOrigin.REPLAY


# --- Broker DTO sanity ----------------------------------------------------


def test_order_request_carries_the_idempotency_key() -> None:
    request = OrderRequest(
        trading_symbol="NIFTY26JAN24500CE",
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        product=Product.NRML,
        order_type=OrderType.LIMIT,
        transaction_type=TransactionType.BUY,
        quantity=75,
        reference_id="ABC12345678",
        price=Decimal("120.50"),
    )
    assert request.reference_id == "ABC12345678"
    assert request.validity is Validity.DAY
    with pytest.raises(Exception):
        request.quantity = 150  # type: ignore[misc]  # frozen


def test_transaction_type_sign_and_opposite() -> None:
    assert TransactionType.BUY.sign == 1
    assert TransactionType.SELL.sign == -1
    assert TransactionType.BUY.opposite is TransactionType.SELL


def test_order_status_terminality() -> None:
    assert OrderStatus.EXECUTED.is_terminal
    assert OrderStatus.REJECTED.is_terminal
    assert OrderStatus.CANCELLED.is_terminal
    assert not OrderStatus.OPEN.is_terminal
    assert OrderStatus.PARTIALLY_FILLED.is_open_at_broker
    assert not OrderStatus.UNKNOWN.is_open_at_broker
