"""PaperBrokerProvider — PAPER-001, PAPER-002, PAPER-007, EXEC-014.

A full ``BrokerProvider`` that never sends an order anywhere. It mirrors the real
broker's constraints deliberately — same products, same order types, same
``DAY``-only validity, same idempotency behaviour on a repeated reference id — so
a strategy that works in paper is not relying on something Groww would refuse.

Everything it returns is marked ``ExecutionRealism.SIMULATED``. That label travels
with every order, trade and position, which is what makes it impossible to report
a paper fill as a real one (PNL-007).

Market data comes from a **live** source by default (PAPER-002): the simulation
is in the execution, not in the prices.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, Sequence

from app.audit.snapshots import freeze_snapshot
from app.brokers.base import BrokerProvider
from app.brokers.groww.capabilities import GROWW_CAPABILITIES, assert_supported
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
from app.brokers.paper.account import PaperAccount, PaperPosition, position_key
from app.brokers.paper.constraints import FillConstraints, database_constraints
from app.brokers.paper.engine import FillConfig, FillEngine
from app.brokers.paper.liquidity import remaining_depth
from app.brokers.paper.state import InMemoryPaperStateStore
from app.brokers.paper.stops import StopActivation
from app.config import BrokerProviderName, Settings, get_settings
from app.core.clock import Clock, get_clock
from app.core.data_origin import ExecutionRealism
from app.core.enums import (
    Exchange,
    InstrumentType,
    MarginEstimateSource,
    OrderStatus,
    OrderType,
    Product,
    Segment,
    TransactionType,
    Validity,
)
from app.core.errors import ConfigurationError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.marketdata.models import InstrumentRef, Quote
from app.portfolio.costs import FeeSchedule, order_costs

logger = get_logger("brokers.paper.provider")

__all__ = ["PaperBrokerProvider", "QuoteSource"]

#: Supplies the current market for an instrument. ``None`` means "unknown", and
#: an unknown price never produces a fill.
QuoteSource = Callable[[InstrumentRef], Awaitable[Optional[Quote]]]


@dataclass
class _SimOrder:
    """A simulated order plus everything needed to re-evaluate it later."""

    broker_order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.OPEN
    filled_quantity: int = 0
    average_fill_price: Optional[Decimal] = None
    rejection_reason: Optional[str] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None
    trades: list[BrokerTrade] = field(default_factory=list)
    fee_schedule: Optional[dict[str, Any]] = None
    eligible_at: datetime | None = None
    constraints: FillConstraints | None = None
    stop_activation: StopActivation | None = None
    stop_activation_known: bool = True

    @property
    def remaining(self) -> int:
        return max(self.request.quantity - self.filled_quantity, 0)

    def to_broker_order(self) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=self.broker_order_id,
            trading_symbol=self.request.trading_symbol,
            exchange=self.request.exchange,
            segment=self.request.segment,
            product=self.request.product,
            order_type=self.request.order_type,
            transaction_type=self.request.transaction_type,
            status=self.status,
            quantity=self.request.quantity,
            filled_quantity=self.filled_quantity,
            remaining_quantity=self.remaining,
            price=self.request.price,
            trigger_price=self.request.trigger_price,
            average_fill_price=self.average_fill_price,
            reference_id=self.request.reference_id,
            rejection_reason=self.rejection_reason,
            created_at=self.created_at,
            exchange_time=self.updated_at,
            raw={
                "simulated": True,
                "stop_activation": self.stop_activation.model_dump(mode="json")
                if self.stop_activation else None,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_activation": self.stop_activation.model_dump(mode="json")
            if self.stop_activation else None,
            "stop_activation_known": self.stop_activation_known,
            "constraints": self.constraints.model_dump(mode="json") if self.constraints else None,
            "eligible_at": self.eligible_at.isoformat() if self.eligible_at else None,
            "fee_schedule": self.fee_schedule,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": (
                str(self.average_fill_price) if self.average_fill_price is not None else None
            ),
            "rejection_reason": self.rejection_reason,
            "request": {
                "trading_symbol": self.request.trading_symbol,
                "exchange": self.request.exchange.value,
                "segment": self.request.segment.value,
                "product": self.request.product.value,
                "order_type": self.request.order_type.value,
                "transaction_type": self.request.transaction_type.value,
                "quantity": self.request.quantity,
                "reference_id": self.request.reference_id,
                "validity": self.request.validity.value,
                "price": str(self.request.price) if self.request.price is not None else None,
                "trigger_price": (
                    str(self.request.trigger_price)
                    if self.request.trigger_price is not None
                    else None
                ),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_SimOrder":
        raw = data["request"]
        activation_known = data.get("stop_activation_known", "stop_activation" in data)
        if type(activation_known) is not bool:
            raise ValidationError("Invalid PAPER stop activation state")
        request = OrderRequest(
            trading_symbol=raw["trading_symbol"],
            exchange=Exchange(raw["exchange"]),
            segment=Segment(raw["segment"]),
            product=Product(raw["product"]),
            order_type=OrderType(raw["order_type"]),
            transaction_type=TransactionType(raw["transaction_type"]),
            quantity=int(raw["quantity"]),
            reference_id=raw["reference_id"],
            validity=Validity(raw.get("validity", "DAY")),
            price=Decimal(raw["price"]) if raw.get("price") else None,
            trigger_price=Decimal(raw["trigger_price"]) if raw.get("trigger_price") else None,
        )
        return cls(
            stop_activation=StopActivation.model_validate(data["stop_activation"])
            if data.get("stop_activation") else None,
            stop_activation_known=activation_known,
            constraints=FillConstraints.model_validate(data["constraints"])
            if data.get("constraints") else None,
            eligible_at=datetime.fromisoformat(data["eligible_at"])
            if data.get("eligible_at")
            else None,
            broker_order_id=data["broker_order_id"],
            fee_schedule=data.get("fee_schedule"),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else None,
            updated_at=datetime.fromisoformat(data["updated_at"])
            if data.get("updated_at")
            else None,
            request=request,
            status=OrderStatus(data["status"]),
            filled_quantity=int(data.get("filled_quantity", 0)),
            average_fill_price=(
                Decimal(data["average_fill_price"]) if data.get("average_fill_price") else None
            ),
            rejection_reason=data.get("rejection_reason"),
        )


class PaperBrokerProvider(BrokerProvider):
    """Simulated broker with real constraints."""

    name = "paper"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        quote_source: Optional[QuoteSource] = None,
        fill_config: Optional[FillConfig] = None,
        clock: Optional[Clock] = None,
        state_store: Optional[Any] = None,
        cost_source=None,
        constraint_source=None,
    ) -> None:
        self._settings = settings or get_settings()
        self._clock = clock or get_clock()
        self._quote_source = quote_source
        self._engine = FillEngine(fill_config)
        self._state_store = state_store or InMemoryPaperStateStore()
        self._cost_source = cost_source
        self._constraint_source = constraint_source

        if self._settings.starting_capital is None:
            raise ConfigurationError(
                "STARTING_CAPITAL must be configured before the paper broker can run. "
                "It has no default on purpose: sizing and the daily loss limit are "
                "expressed against it."
            )
        self._account = PaperAccount(starting_capital=self._settings.starting_capital)

        self._orders: dict[str, _SimOrder] = {}
        self._reference_index: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._costs_modelled = False

    # --- Identity ---------------------------------------------------------

    @property
    def execution_realism(self) -> ExecutionRealism:
        return ExecutionRealism.SIMULATED

    @property
    def capabilities(self) -> BrokerCapabilities:
        # Mirrors Groww so paper behaviour cannot depend on something the real
        # broker would refuse.
        caps = GROWW_CAPABILITIES
        return BrokerCapabilities(
            name="paper",
            supported_exchanges=caps.supported_exchanges,
            supported_segments=caps.supported_segments,
            supported_products=caps.supported_products,
            supported_order_types=caps.supported_order_types,
            supported_validities=caps.supported_validities,
            supports_bracket_orders=caps.supports_bracket_orders,
            supports_gtt=caps.supports_gtt,
            supports_amo=caps.supports_amo,
            max_batch_quote_symbols=caps.max_batch_quote_symbols,
            max_feed_subscriptions=caps.max_feed_subscriptions,
            max_orders_per_second=caps.max_orders_per_second,
            max_orders_per_minute=caps.max_orders_per_minute,
        )

    @property
    def account(self) -> PaperAccount:
        return self._account

    def set_quote_source(self, source: QuoteSource) -> None:
        self._quote_source = source

    # --- Connection -------------------------------------------------------

    async def connect(self) -> None:
        await self.restore()
        if not self._costs_modelled:
            # Said once, loudly: paper P&L is gross until the cost model lands.
            logger.warning(
                "PAPER fees require an explicit effective tariff. Missing tariffs remain "
                "UNAVAILABLE; known charges are estimates, not broker billing verification."
            )

    async def close(self) -> None:
        await self.persist()

    async def ping(self) -> bool:
        return True

    async def get_profile(self) -> BrokerProfile:
        return BrokerProfile(
            account_id="paper-account",
            name="Simulated account",
            raw={"simulated": True, "starting_capital": str(self._account.starting_capital)},
        )

    # --- Persistence ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "account": self._account.to_dict(),
            "orders": {oid: order.to_dict() for oid, order in self._orders.items()},
            "trades": [
                {
                    "broker_order_id": trade.broker_order_id,
                    "quantity": trade.quantity,
                    "price": str(trade.price),
                    "exchange_trade_id": trade.exchange_trade_id,
                    "raw": trade.raw,
                    "trading_symbol": trade.trading_symbol,
                    "executed_at": trade.executed_at.isoformat() if trade.executed_at else None,
                }
                for order in self._orders.values()
                for trade in order.trades
            ],
            "reference_index": dict(self._reference_index),
            "fill_config": {
                "slippage_bps": str(self._engine.config.slippage_bps),
                "latency_ms": self._engine.config.latency_ms,
                "seed": self._engine.config.seed,
                "reject_probability": self._engine.config.reject_probability,
                "partial_fill_probability": self._engine.config.partial_fill_probability,
                "use_depth": self._engine.config.use_depth,
                "rng_state": self._engine._rng.getstate(),
            },
        }

    async def persist(self) -> None:
        await self._state_store.save(self.snapshot())

    async def restore(self) -> None:
        stored = await self._state_store.load()
        if not stored:
            return
        if stored.get("account"):
            self._account = PaperAccount.from_dict(stored["account"])
        config = stored.get("fill_config")
        if config:
            self._engine = FillEngine(
                FillConfig(
                    slippage_bps=Decimal(config["slippage_bps"]),
                    latency_ms=config["latency_ms"],
                    seed=config["seed"],
                    reject_probability=config.get("reject_probability", 0),
                    partial_fill_probability=config.get("partial_fill_probability", 0),
                    use_depth=config.get("use_depth", True),
                )
            )
            if config.get("rng_state"):
                version, internal, gaussian = config["rng_state"]
                self._engine._rng.setstate((version, tuple(internal), gaussian))
        self._orders = {
            oid: _SimOrder.from_dict(data) for oid, data in (stored.get("orders") or {}).items()
        }
        self._reference_index = dict(stored.get("reference_index") or {})
        for raw in stored.get("trades") or []:
            order = self._orders.get(raw["broker_order_id"])
            if order is None:
                raise ValidationError("Persisted paper fill has no corresponding order")
            order.trades.append(
                BrokerTrade(
                    broker_order_id=order.broker_order_id,
                    quantity=int(raw["quantity"]),
                    price=Decimal(raw["price"]),
                    executed_at=datetime.fromisoformat(raw["executed_at"])
                    if raw.get("executed_at")
                    else None,
                    exchange_trade_id=raw.get("exchange_trade_id"),
                    trading_symbol=order.request.trading_symbol,
                    segment=order.request.segment,
                    raw=raw.get("raw", {"simulated": True}),
                )
            )
        if any(
            sum(fill.quantity for fill in order.trades) != order.filled_quantity
            for order in self._orders.values()
        ):
            raise ValidationError("Persisted paper fills do not match order quantities")
        self._verify_option_positions()
        logger.info(
            "Restored paper broker state",
            extra={
                "orders": len(self._orders),
                "open_positions": len(self._account.open_positions()),
                "equity": str(self._account.equity),
            },
        )

    def _verify_option_positions(self):
        linked = {}
        for order in self._orders.values():
            if not order.filled_quantity:
                continue
            key = position_key(
                order.request.trading_symbol, order.request.segment, order.request.product
            )
            linked.setdefault(key, []).append(order)
            if order.constraints and order.constraints.instrument_type == InstrumentType.OPTION:
                position = self._account.positions.get(key)
                if position is None or position.instrument_type != InstrumentType.OPTION:
                    raise ValidationError("Persisted option identity missing or changed")
        for position in self._account.positions.values():
            if position.instrument_type != InstrumentType.OPTION:
                continue
            orders = linked.get(position.key, [])
            invalid_types = any(
                order.constraints is None
                or order.constraints.instrument_type != InstrumentType.OPTION
                for order in orders
            )
            quantity = sum(
                order.filled_quantity * order.request.transaction_type.sign for order in orders
            )
            if not orders or invalid_types or quantity != position.net_quantity:
                raise ValidationError("Persisted option position disagrees with order fills")

    # --- Orders -----------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderAck:
        async with self._lock:
            assert_supported(
                exchange=request.exchange,
                segment=request.segment,
                product=request.product,
                order_type=request.order_type,
                validity=request.validity,
            )
            if type(request.quantity) is not int or request.quantity <= 0:
                raise ValidationError(f"quantity must be positive, got {request.quantity}")

            # Idempotency, matching Groww's duplicate-reference behaviour: the
            # same intent submitted twice yields one order, not two.
            existing_id = self._reference_index.get(request.reference_id)
            if existing_id is not None:
                existing = self._orders[existing_id]
                logger.info(
                    "Duplicate order reference; returning the existing order",
                    extra={
                        "reference_id": request.reference_id,
                        "broker_order_id": existing_id,
                    },
                )
                return OrderAck(
                    broker_order_id=existing_id,
                    reference_id=request.reference_id,
                    status=existing.status,
                    remark="duplicate order_reference_id; existing order returned",
                    raw={"simulated": True, "duplicate": True},
                )

            order = _SimOrder(
                eligible_at=self._clock.now()
                + timedelta(milliseconds=self._engine.config.latency_ms),
                broker_order_id=new_id("sim"),
                request=request,
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            if self._cost_source:
                schedule = await self._cost_source(request, self._clock.now())
                order.fee_schedule = schedule.model_dump(mode="json") if schedule else None
            if self._constraint_source:
                order.constraints = await self._constraint_source(request, self._clock.now())
            self._orders[order.broker_order_id] = order
            self._reference_index[request.reference_id] = order.broker_order_id

            quote = await self._quote_for(request)
            reference_price = self._reference_price(request, quote)
            position = self._account.positions.get(
                position_key(request.trading_symbol, request.segment, request.product)
            )
            reducing = (
                min(request.quantity, abs(position.net_quantity))
                if (
                    position is not None
                    and position.net_quantity * request.transaction_type.sign < 0
                )
                else 0
            )
            increasing = request.quantity - reducing
            if (
                reference_price is not None
                and increasing
                and not self._account.can_afford(
                    increasing, reference_price, request.product, request.segment,
                    order.constraints.instrument_type if order.constraints else None,
                )
            ):
                required = self._account.requirement_for(
                    increasing, reference_price, request.product, request.segment,
                    order.constraints.instrument_type if order.constraints else None,
                )
                order.status = OrderStatus.REJECTED
                order.rejection_reason = (
                    f"insufficient margin: {required} required, "
                    f"{self._account.available_margin} available"
                )
                logger.info(
                    "Paper order rejected for margin",
                    extra={
                        "required": str(required),
                        "available": str(self._account.available_margin),
                    },
                )
                await self.persist()
                return OrderAck(
                    broker_order_id=order.broker_order_id,
                    reference_id=request.reference_id,
                    status=OrderStatus.REJECTED,
                    remark=order.rejection_reason,
                    raw={"simulated": True},
                )

            await self._evaluate(order, quote)
            await self.persist()

            return OrderAck(
                broker_order_id=order.broker_order_id,
                reference_id=request.reference_id,
                status=order.status,
                remark=order.rejection_reason,
                raw={"simulated": True},
            )

    async def modify_order(self, request: ModifyRequest) -> OrderAck:
        async with self._lock:
            order = self._require(request.broker_order_id)
            if order.status.is_terminal:
                raise ValidationError(
                    f"Order {order.broker_order_id} is already {order.status.value}; "
                    f"it cannot be modified"
                )

            updated = order.request
            changes: dict[str, Any] = {}
            if request.quantity is not None:
                if type(request.quantity) is not int or request.quantity <= 0:
                    raise ValidationError("modified quantity must be a positive integer")
                if request.quantity < order.filled_quantity:
                    raise ValidationError(
                        f"cannot reduce quantity below the filled quantity "
                        f"({order.filled_quantity})"
                    )
                changes["quantity"] = request.quantity
            if request.order_type is not None:
                changes["order_type"] = request.order_type
            if request.price is not None:
                changes["price"] = request.price
            if request.trigger_price is not None:
                changes["trigger_price"] = request.trigger_price
            if not changes:
                raise ValidationError("a modify request must change at least one field")

            if order.stop_activation is not None and (
                request.order_type is not None and request.order_type != updated.order_type
                or request.trigger_price is not None and request.trigger_price != updated.trigger_price
            ):
                raise ValidationError("activated stop trigger/type cannot change; cancel and replace")

            order.request = _replace_request(updated, **changes)
            order.eligible_at = self._clock.now() + timedelta(
                milliseconds=self._engine.config.latency_ms
            )
            order.updated_at = self._clock.now()

            await self._evaluate(order, await self._quote_for(order.request))
            await self.persist()
            return OrderAck(
                broker_order_id=order.broker_order_id,
                reference_id=order.request.reference_id,
                status=order.status,
                raw={"simulated": True},
            )

    async def cancel_order(self, broker_order_id: str, segment: Segment) -> OrderAck:
        async with self._lock:
            order = self._require(broker_order_id)
            if order.status is OrderStatus.CANCELLED:
                # Idempotent, like the real adapter.
                return OrderAck(
                    broker_order_id=broker_order_id,
                    reference_id=order.request.reference_id,
                    status=OrderStatus.CANCELLED,
                    remark="already cancelled",
                    raw={"simulated": True},
                )
            if order.status.is_terminal:
                raise ValidationError(
                    f"Order {broker_order_id} is {order.status.value} and cannot be cancelled"
                )

            order.status = (
                OrderStatus.CANCELLED if order.filled_quantity == 0 else OrderStatus.CANCELLED
            )
            order.updated_at = self._clock.now()
            await self.persist()
            return OrderAck(
                broker_order_id=broker_order_id,
                reference_id=order.request.reference_id,
                status=OrderStatus.CANCELLED,
                raw={"simulated": True},
            )

    async def get_order(self, broker_order_id: str, segment: Segment) -> BrokerOrder:
        return self._require(broker_order_id).to_broker_order()

    async def get_order_by_reference(
        self, reference_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        broker_order_id = self._reference_index.get(reference_id)
        if broker_order_id is None:
            return None
        return self._orders[broker_order_id].to_broker_order()

    async def list_orders(self, segment: Optional[Segment] = None) -> Sequence[BrokerOrder]:
        return [
            order.to_broker_order()
            for order in self._orders.values()
            if segment is None or order.request.segment is segment
        ]

    async def list_trades(self, broker_order_id: str, segment: Segment) -> Sequence[BrokerTrade]:
        return list(self._require(broker_order_id).trades)

    # --- Portfolio --------------------------------------------------------

    async def get_positions(self) -> Sequence[BrokerPosition]:
        return [self._to_broker_position(p) for p in self._account.positions.values()]

    async def get_holdings(self) -> Sequence[BrokerHolding]:
        """Delivery positions present as holdings, as they would at a real broker."""
        return [
            BrokerHolding(
                trading_symbol=position.trading_symbol,
                isin=None,
                quantity=position.net_quantity,
                average_price=position.average_price,
                last_price=position.last_price,
                raw={"simulated": True},
            )
            for position in self._account.positions.values()
            if position.product is Product.CNC and position.net_quantity > 0
        ]

    async def get_margin(self) -> MarginInfo:
        return MarginInfo(
            available_margin=self._account.available_margin,
            used_margin=self._account.used_margin,
            cash=self._account.cash,
            # The requirement is a local estimate, and says so (FUT-004).
            source=MarginEstimateSource.ESTIMATED,
            raw={"simulated": True, "equity": str(self._account.equity)},
        )

    # --- Simulation -------------------------------------------------------

    def _fill_costs(self, order, fill):
        if order.fee_schedule is None:
            return Decimal(0), {"status": "UNAVAILABLE", "pnl_basis": "GROSS"}
        schedule = FeeSchedule.model_validate(order.fee_schedule)
        if (schedule.exchange, schedule.segment, schedule.product) != (
            order.request.exchange, order.request.segment, order.request.product
        ):
            raise ValidationError("fee schedule scope disagrees with order")
        if schedule.charge_basis == "OPTION_PREMIUM" and (
            order.constraints is None
            or order.constraints.instrument_type != InstrumentType.OPTION
        ):
            raise ValidationError("option premium fees require captured option identity")
        previous = sum((trade.price * trade.quantity for trade in order.trades), Decimal(0))
        before = order_costs(
            schedule, previous, order.request.transaction_type, as_of=self._clock.now()
        )
        after = order_costs(
            schedule,
            previous + fill.price * fill.quantity,
            order.request.transaction_type,
            as_of=self._clock.now(),
        )
        delta = {key: after[key] - before[key] for key in after}
        if any(value < 0 for value in delta.values()):
            raise ValidationError("incremental fill fees cannot be negative")
        return delta["total"], freeze_snapshot(
            {
                "status": "ESTIMATED",
                "pnl_basis": "NET_ESTIMATED",
                "schedule": order.fee_schedule,
                "components": delta,
                "cumulative": after,
                "turnover": previous + fill.price * fill.quantity,
            }
        )

    def _has_execution_margin(self, order, fills):
        account = deepcopy(self._account)
        preview = deepcopy(order)
        for fill in fills:
            key = position_key(
                order.request.trading_symbol, order.request.segment, order.request.product
            )
            position = account.positions.get(key)
            quantity = position.net_quantity if position is not None else 0
            increasing = quantity * order.request.transaction_type.sign >= 0 or fill.quantity > abs(
                quantity
            )
            charges, _ = self._fill_costs(preview, fill)
            account.apply_fill(
                trading_symbol=order.request.trading_symbol,
                segment=order.request.segment,
                product=order.request.product,
                transaction_type=order.request.transaction_type,
                quantity=fill.quantity,
                price=fill.price,
                charges=charges,
                instrument_type=order.constraints.instrument_type if order.constraints else None,
            )
            if increasing and account.available_margin < 0:
                return False
            preview.trades.append(
                BrokerTrade(
                    broker_order_id=order.broker_order_id,
                    quantity=fill.quantity,
                    price=fill.price,
                    executed_at=self._clock.now(),
                    exchange_trade_id=None,
                )
            )
        return True

    def _eligible_at(self, order):
        if order.eligible_at is not None:
            return order.eligible_at
        if order.created_at is None:
            raise ValidationError("PAPER order creation time unavailable; execution refused")
        return order.created_at + timedelta(milliseconds=self._engine.config.latency_ms)

    @property
    def next_execution_at(self):
        pending = [
            self._eligible_at(order)
            for order in self._orders.values()
            if not order.status.is_terminal and self._eligible_at(order) > self._clock.now()
        ]
        return min(pending) if pending else None

    async def settle_open_orders(self) -> list[BrokerOrder]:
        """Re-evaluate resting orders against the current market.

        This is what turns a stop into a fill when price reaches the trigger. The
        trading loop calls it each cycle; without it, resting orders would never
        execute in paper mode.
        """
        async with self._lock:
            changed: list[BrokerOrder] = []
            for identifier in sorted(self._orders):
                order = self._orders[identifier]
                if order.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                    continue
                before = (order.status, order.filled_quantity, order.stop_activation)
                await self._evaluate(order, await self._quote_for(order.request))
                if (order.status, order.filled_quantity, order.stop_activation) != before:
                    changed.append(order.to_broker_order())
            if changed:
                await self.persist()
            return changed

    def _option_contract_error(self, order):
        request = order.request
        position = self._account.positions.get(
            position_key(request.trading_symbol, request.segment, request.product)
        )
        kind = order.constraints.instrument_type if order.constraints else None
        if position and position.instrument_type is not None:
            if kind is not None and kind != position.instrument_type:
                return "PAPER position instrument type changed"
            kind = position.instrument_type
        if kind != InstrumentType.OPTION:
            return None
        if request.segment != Segment.FNO or request.product == Product.CNC:
            return "Invalid option product or segment"
        if position and position.net_quantity and position.instrument_type is None:
            return "Legacy option position requires source reconstruction"
        held = position.net_quantity if position else 0
        if request.transaction_type == TransactionType.SELL and order.remaining > max(0, held):
            return "NAKED_SHORT_DISABLED"
        return (
            order.constraints.option_blocker(self._clock.now())
            if order.constraints else "OPTION_CONTRACT_UNAVAILABLE"
        )

    async def _evaluate(self, order: _SimOrder, quote: Optional[Quote]) -> None:
        option_error = self._option_contract_error(order)
        if option_error:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = option_error
            order.updated_at = self._clock.now()
            return
        await self._evaluate_ready(order, quote)

    async def _evaluate_ready(self, order: _SimOrder, quote: Quote | None) -> None:
        if order.request.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            if not order.stop_activation_known:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "legacy PAPER stop activation unavailable; cancel and replace"
                order.updated_at = self._clock.now()
                return
        if order.stop_activation is not None:
            order.stop_activation.require_for(order.request, self._clock.now())
        if self._constraint_source and order.constraints is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "PAPER instrument constraints unavailable; no fill permitted"
            order.updated_at = self._clock.now()
            return
        if order.constraints is not None:
            identity = InstrumentRef(
                order.request.trading_symbol, order.request.exchange, order.request.segment
            ).key
            if (
                order.constraints.instrument_key != identity
                or order.constraints.captured_at > self._clock.now()
            ):
                raise ValidationError("Invalid PAPER instrument constraint identity or timestamp")
        if self._clock.now() < self._eligible_at(order):
            order.rejection_reason = "simulated submission latency; order remains open"
            return
        if quote is not None and quote.observed_at > self._clock.now():
            order.rejection_reason = "future market data; order remains open"
            return
        liquidity = None
        if quote is not None:
            quote, liquidity = remaining_depth(
                quote, order.request, use_depth=self._engine.config.use_depth,
                orders=self._orders.values(),
            )
        outcome = self._engine.simulate(
            order_type=order.request.order_type,
            transaction_type=order.request.transaction_type,
            quantity=order.remaining,
            limit_price=order.request.price,
            trigger_price=order.request.trigger_price,
            quote=quote,
            constraints=order.constraints,
            stop_triggered=order.stop_activation is not None,
        )

        if outcome.status is OrderStatus.REJECTED:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = outcome.reason
            order.updated_at = self._clock.now()
            return

        if outcome.stop_triggered and order.stop_activation is None:
            order.stop_activation = StopActivation(
                instrument_key=quote.instrument.key,
                activated_at=self._clock.now(), observed_at=quote.observed_at,
                data_origin=quote.data_origin, transaction_type=order.request.transaction_type,
                ltp=quote.ltp, trigger_price=order.request.trigger_price,
            )
            try:
                await self.persist()
            except Exception:
                order.stop_activation = None
                raise

        if outcome.fills and not self._has_execution_margin(order, outcome.fills):
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "insufficient margin at simulated execution"
            order.updated_at = self._clock.now()
            return
        for fill in outcome.fills:
            charges, breakdown = self._fill_costs(order, fill)
            self._account.apply_fill(
                trading_symbol=order.request.trading_symbol,
                segment=order.request.segment,
                product=order.request.product,
                transaction_type=order.request.transaction_type,
                quantity=fill.quantity,
                price=fill.price,
                charges=charges,
                instrument_type=order.constraints.instrument_type if order.constraints else None,
            )
            trade = BrokerTrade(
                broker_order_id=order.broker_order_id,
                quantity=fill.quantity,
                price=fill.price,
                executed_at=self._clock.now(),
                exchange_trade_id=new_id("simtrd"),
                trading_symbol=order.request.trading_symbol,
                segment=order.request.segment,
                raw={
                    "simulated": True, "costs": breakdown, "liquidity": liquidity,
                    "instrument_constraints": order.constraints.model_dump(mode="json")
                    if order.constraints else None,
                    "stop_activation": order.stop_activation.model_dump(mode="json")
                    if order.stop_activation else None,
                },
            )
            order.trades.append(trade)

        if outcome.fills:
            total_quantity = sum(t.quantity for t in order.trades)
            total_value = sum(t.price * t.quantity for t in order.trades)
            order.filled_quantity = total_quantity
            order.average_fill_price = quantize_money(total_value / total_quantity)

        order.status = (
            OrderStatus.EXECUTED
            if order.filled_quantity >= order.request.quantity
            else (OrderStatus.PARTIALLY_FILLED if order.filled_quantity else OrderStatus.OPEN)
        )
        order.rejection_reason = outcome.reason if order.status is OrderStatus.OPEN else None
        order.updated_at = self._clock.now()

    async def _quote_for(self, request: OrderRequest) -> Optional[Quote]:
        if self._quote_source is None:
            return None
        ref = InstrumentRef(
            trading_symbol=request.trading_symbol,
            exchange=request.exchange,
            segment=request.segment,
        )
        try:
            return await self._quote_source(ref)
        except Exception as exc:  # noqa: BLE001 - a data failure is not a fill
            logger.warning(
                "Quote source failed; the order cannot be evaluated this cycle",
                extra={"trading_symbol": request.trading_symbol, "error": str(exc)},
            )
            return None

    def _reference_price(self, request: OrderRequest, quote: Optional[Quote]) -> Optional[Decimal]:
        if request.price is not None:
            return request.price
        if quote is not None:
            return quote.ltp
        return None

    def _require(self, broker_order_id: str) -> _SimOrder:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise NotFoundError(
                f"No simulated order with id {broker_order_id}",
                context={"broker_order_id": broker_order_id},
            )
        return order

    def _to_broker_position(self, position: PaperPosition) -> BrokerPosition:
        exchanges = {
            order.request.exchange
            for order in self._orders.values()
            if order.filled_quantity
            and (order.request.trading_symbol, order.request.segment, order.request.product)
            == (position.trading_symbol, position.segment, position.product)
        }
        if len(exchanges) != 1:
            raise ValidationError("PAPER position exchange lineage unavailable or ambiguous")
        return BrokerPosition(
            trading_symbol=position.trading_symbol,
            exchange=exchanges.pop(),
            segment=position.segment,
            product=position.product,
            net_quantity=position.net_quantity,
            average_price=position.average_price,
            bought_quantity=position.bought_quantity,
            sold_quantity=position.sold_quantity,
            realised_pnl=position.realised_pnl,
            unrealised_pnl=(
                quantize_money(
                    (position.last_price - position.average_price) * position.net_quantity
                )
                if position.last_price is not None and position.net_quantity
                else None
            ),
            last_price=position.last_price,
            raw={"simulated": True},
        )


def _replace_request(request: OrderRequest, **changes: Any) -> OrderRequest:
    """``dataclasses.replace`` for the frozen request."""
    from dataclasses import replace

    return replace(request, **changes)


def _build(settings: Settings) -> BrokerProvider:
    return PaperBrokerProvider(settings, constraint_source=database_constraints)


def register() -> None:
    from app.brokers.factory import register_broker_provider

    register_broker_provider(BrokerProviderName.PAPER, _build)


register()
