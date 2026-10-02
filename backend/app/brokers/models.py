"""Broker-facing data transfer objects.

These are the *normalised* shapes the rest of the system works with. A broker
adapter's job is to translate its wire format into these; nothing above the
adapter layer ever sees a Groww-specific dict.

They are plain frozen dataclasses rather than ORM models because they describe
what the broker said at a point in time, which is not the same thing as what we
have persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from app.core.enums import (
    Exchange,
    MarginEstimateSource,
    OrderStatus,
    OrderType,
    Product,
    Segment,
    TransactionType,
    Validity,
)

__all__ = [
    "OrderRequest",
    "ModifyRequest",
    "OrderAck",
    "BrokerOrder",
    "BrokerTrade",
    "BrokerPosition",
    "BrokerHolding",
    "MarginInfo",
    "BrokerProfile",
    "BrokerCapabilities",
]


@dataclass(frozen=True)
class OrderRequest:
    """Everything needed to place one order.

    ``reference_id`` is the idempotency key: it is derived deterministically from
    the intent id, so re-submitting the same intent produces the same value and
    the broker rejects the duplicate (Groww ``GA007``) rather than creating a
    second order.
    """

    trading_symbol: str
    exchange: Exchange
    segment: Segment
    product: Product
    order_type: OrderType
    transaction_type: TransactionType
    quantity: int
    reference_id: str
    validity: Validity = Validity.DAY
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    algo_id: Optional[str] = None
    #: Free-form local metadata; never sent to the broker.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModifyRequest:
    broker_order_id: str
    segment: Segment
    quantity: Optional[int] = None
    order_type: Optional[OrderType] = None
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None


@dataclass(frozen=True)
class OrderAck:
    """Immediate response to a placement request."""

    broker_order_id: Optional[str]
    reference_id: Optional[str]
    status: OrderStatus
    remark: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrder:
    """An order as the broker currently sees it."""

    broker_order_id: str
    trading_symbol: str
    exchange: Exchange
    segment: Segment
    product: Product
    order_type: OrderType
    transaction_type: TransactionType
    status: OrderStatus
    quantity: int
    filled_quantity: int = 0
    remaining_quantity: Optional[int] = None
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    average_fill_price: Optional[Decimal] = None
    reference_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    exchange_time: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerTrade:
    """A single fill reported by the broker."""

    broker_order_id: str
    quantity: int
    price: Decimal
    executed_at: Optional[datetime] = None
    exchange_trade_id: Optional[str] = None
    trading_symbol: Optional[str] = None
    isin: Optional[str] = None
    segment: Optional[Segment] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPosition:
    """Net position as reported by the broker. Signed quantity: short is negative."""

    trading_symbol: str
    exchange: Exchange
    segment: Segment
    product: Product
    net_quantity: int
    average_price: Decimal
    bought_quantity: int = 0
    sold_quantity: int = 0
    realised_pnl: Optional[Decimal] = None
    unrealised_pnl: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerHolding:
    """Delivery holding (CNC), distinct from an intraday position."""

    trading_symbol: str
    isin: Optional[str]
    quantity: int
    average_price: Decimal
    pledged_quantity: int = 0
    demat_free_quantity: Optional[int] = None
    last_price: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarginInfo:
    """Funds and margin.

    ``source`` records whether the figures came from the broker or were estimated
    locally. Estimated margins carry a safety multiplier before the risk engine
    uses them (FUT-004) — an underestimate would allow a position the account
    cannot actually support.
    """

    available_margin: Decimal
    used_margin: Optional[Decimal] = None
    total_collateral: Optional[Decimal] = None
    cash: Optional[Decimal] = None
    source: MarginEstimateSource = MarginEstimateSource.BROKER
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerProfile:
    """Identity of the account the adapter is authenticated against."""

    account_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerCapabilities:
    """What this broker actually supports (GRW-025).

    Declared explicitly so higher layers fail fast and locally instead of
    discovering an unsupported feature via a rejected order.
    """

    name: str
    supported_exchanges: frozenset[Exchange]
    supported_segments: frozenset[Segment]
    supported_products: frozenset[Product]
    supported_order_types: frozenset[OrderType]
    supported_validities: frozenset[Validity]
    supports_bracket_orders: bool = False
    supports_gtt: bool = False
    supports_amo: bool = False
    max_batch_quote_symbols: int = 50
    max_feed_subscriptions: int = 1000
    max_orders_per_second: int = 10
    max_orders_per_minute: int = 250
