"""Fill simulation — PAPER-004, EXEC-014, BT-003.

Fills use the supplied depth snapshot when enabled and available. This is a
simplified simulation, not a reproduction of exchange queue priority, market
impact or subsequent book replenishment. Submission latency is enforced by the
PAPER provider's injected clock, not by sleeping inside this price calculator.

Three rules:

1. **No price is ever invented.** With no market data the order simply does not
   fill; it stays open. Inventing a fill price would manufacture P&L.
2. **Deterministic.** All randomness comes from a seeded generator, so a paper
   session or backtest replays identically (BT-008).
3. **Everything produced here is labelled ``SIMULATED``** and can never be
   reported as a broker fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from random import Random
from typing import Optional, Sequence

from app.brokers.paper.constraints import FillConstraints
from app.core.enums import OrderStatus, OrderType, TransactionType
from app.core.money import quantize_money
from app.marketdata.circuits import circuit_status
from app.marketdata.models import DepthLevel, Quote

__all__ = ["FillConfig", "SimulatedFill", "FillOutcome", "FillEngine"]


@dataclass(frozen=True)
class FillConfig:
    """Execution realism knobs. Defaults are deliberately pessimistic."""

    #: Adverse price movement applied when filling without a depth book.
    slippage_bps: Decimal = Decimal("5")
    latency_ms: int = 150
    #: Probability the broker rejects an order outright.
    reject_probability: float = 0.0
    #: Probability a marketable order fills only partially despite available depth.
    partial_fill_probability: float = 0.0
    #: Walk the depth ladder when present; otherwise fill at LTP plus slippage.
    use_depth: bool = True
    seed: int = 1337

    def __post_init__(self):
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("latency_ms must be a nonnegative integer")


@dataclass(frozen=True)
class SimulatedFill:
    quantity: int
    price: Decimal


@dataclass
class FillOutcome:
    status: OrderStatus
    fills: list[SimulatedFill] = field(default_factory=list)
    reason: Optional[str] = None
    stop_triggered: bool = False

    @property
    def filled_quantity(self) -> int:
        return sum(fill.quantity for fill in self.fills)

    @property
    def average_price(self) -> Optional[Decimal]:
        if not self.fills:
            return None
        total = sum(fill.price * fill.quantity for fill in self.fills)
        return quantize_money(total / self.filled_quantity)


class FillEngine:
    def __init__(self, config: Optional[FillConfig] = None) -> None:
        self.config = config or FillConfig()
        self._rng = Random(self.config.seed)

    def reset(self) -> None:
        """Re-seed, so a replay starts from the same random stream."""
        self._rng = Random(self.config.seed)

    # --- Entry point ------------------------------------------------------

    def simulate(
        self,
        *,
        order_type: OrderType,
        transaction_type: TransactionType,
        quantity: int,
        limit_price: Optional[Decimal],
        trigger_price: Optional[Decimal],
        quote: Optional[Quote],
        constraints: Optional[FillConstraints] = None,
        stop_triggered: bool = False,
    ) -> FillOutcome:
        """Decide what happens to an order given the current market."""
        if type(quantity) is not int or quantity <= 0:
            return FillOutcome(status=OrderStatus.REJECTED, reason="invalid fill quantity")
        if quote is None:
            return FillOutcome(
                status=OrderStatus.OPEN,
                reason="no market data available; order remains open",
            )

        prices = [quote.ltp, *(level.price for level in (*quote.bids, *quote.asks))]
        prices.extend(price for price in (limit_price, trigger_price) if price is not None)
        circuit = circuit_status(quote, prices)
        if circuit not in {"AVAILABLE", "UNAVAILABLE"}:
            return FillOutcome(status=OrderStatus.REJECTED, reason=circuit)
        if constraints is not None:
            if quantity % constraints.lot_size or any(
                not constraints.on_tick(price) for price in prices
            ):
                return FillOutcome(
                    status=OrderStatus.REJECTED, reason="invalid lot quantity or off-tick price"
                )

        if self.config.reject_probability and self._rng.random() < self.config.reject_probability:
            return FillOutcome(status=OrderStatus.REJECTED, reason="simulated broker rejection")

        # Stop orders do nothing until their trigger is crossed.
        if order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            if trigger_price is None:
                return FillOutcome(
                    status=OrderStatus.REJECTED, reason="stop order without a trigger price"
                )
            if not stop_triggered and not _trigger_crossed(transaction_type, trigger_price, quote.ltp):
                return FillOutcome(status=OrderStatus.OPEN, reason="trigger not reached")
            effective_type = (
                OrderType.MARKET if order_type is OrderType.STOP_LOSS_MARKET else OrderType.LIMIT
            )
        else:
            effective_type = order_type

        if effective_type is OrderType.MARKET:
            outcome = self._fill_market(transaction_type, quantity, quote, constraints)
        else:
            outcome = self._fill_limit(transaction_type, quantity, limit_price, quote, constraints)
        outcome.stop_triggered = order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET)
        circuit = circuit_status(quote, [fill.price for fill in outcome.fills])
        if circuit not in {"AVAILABLE", "UNAVAILABLE"}:
            return FillOutcome(status=OrderStatus.REJECTED, reason=circuit)
        return outcome

    # --- Market -----------------------------------------------------------

    def _fill_market(
        self,
        transaction_type: TransactionType,
        quantity: int,
        quote: Quote,
        constraints: Optional[FillConstraints],
    ) -> FillOutcome:
        levels = self._levels(transaction_type, quote)
        lot_size = constraints.lot_size if constraints else 1
        target = self._apply_partial(quantity, lot_size)

        if self.config.use_depth and levels:
            fills = _walk_book(levels, target, lot_size)
            filled = sum(f.quantity for f in fills)
            if filled == 0:
                return FillOutcome(status=OrderStatus.OPEN, reason="no depth available")
            if filled < quantity:
                # Remaining size could not be filled from the visible book.
                return FillOutcome(
                    status=OrderStatus.PARTIALLY_FILLED,
                    fills=fills,
                    reason="insufficient depth for the full quantity",
                )
            return FillOutcome(status=OrderStatus.EXECUTED, fills=fills)

        price = _apply_slippage(quote.ltp, transaction_type, self.config.slippage_bps, constraints)
        if price <= 0:
            return FillOutcome(status=OrderStatus.REJECTED, reason="invalid simulated fill price")
        fills = [SimulatedFill(quantity=target, price=price)]
        status = OrderStatus.EXECUTED if target == quantity else OrderStatus.PARTIALLY_FILLED
        return FillOutcome(status=status, fills=fills)

    # --- Limit ------------------------------------------------------------

    def _fill_limit(
        self,
        transaction_type: TransactionType,
        quantity: int,
        limit_price: Optional[Decimal],
        quote: Quote,
        constraints: Optional[FillConstraints],
    ) -> FillOutcome:
        if limit_price is None:
            return FillOutcome(status=OrderStatus.REJECTED, reason="limit order without a price")

        levels = self._levels(transaction_type, quote)
        best = levels[0].price if levels else quote.ltp

        marketable = (
            best <= limit_price if transaction_type is TransactionType.BUY else best >= limit_price
        )
        if not marketable:
            return FillOutcome(
                status=OrderStatus.OPEN,
                reason=f"limit {limit_price} not marketable against {best}",
            )

        lot_size = constraints.lot_size if constraints else 1
        target = self._apply_partial(quantity, lot_size)

        if self.config.use_depth and levels:
            # Only levels at or better than the limit can fill.
            eligible = [
                level
                for level in levels
                if (level.price <= limit_price) == (transaction_type is TransactionType.BUY)
                or level.price == limit_price
            ]
            fills = _walk_book(eligible, target, lot_size)
        else:
            # Price improvement is not simulated without a book: fill at the limit.
            fills = [SimulatedFill(quantity=target, price=limit_price)]

        filled = sum(f.quantity for f in fills)
        if filled == 0:
            return FillOutcome(status=OrderStatus.OPEN, reason="no eligible depth at the limit")
        if filled < quantity:
            return FillOutcome(
                status=OrderStatus.PARTIALLY_FILLED,
                fills=fills,
                reason="insufficient depth at or better than the limit",
            )
        return FillOutcome(status=OrderStatus.EXECUTED, fills=fills)

    # --- Helpers ----------------------------------------------------------

    def _levels(self, transaction_type: TransactionType, quote: Quote) -> Sequence[DepthLevel]:
        """A buy consumes asks; a sell consumes bids."""
        return quote.asks if transaction_type is TransactionType.BUY else quote.bids

    def _apply_partial(self, quantity: int, lot_size: int) -> int:
        if (
            self.config.partial_fill_probability
            and self._rng.random() < self.config.partial_fill_probability
        ):
            # Fill somewhere between a third and all but one unit.
            return max(lot_size, int(quantity * self._rng.uniform(0.34, 0.99)) // lot_size * lot_size)
        return quantity


def _trigger_crossed(
    transaction_type: TransactionType, trigger: Decimal, last_price: Decimal
) -> bool:
    """A buy stop triggers above the trigger; a sell stop triggers below it."""
    if transaction_type is TransactionType.BUY:
        return last_price >= trigger
    return last_price <= trigger


def _apply_slippage(
    price: Decimal,
    transaction_type: TransactionType,
    slippage_bps: Decimal,
    constraints: Optional[FillConstraints] = None,
) -> Decimal:
    """Slippage always works against the order."""
    adjustment = price * slippage_bps / Decimal(10_000)
    if constraints is not None:
        buying = transaction_type is TransactionType.BUY
        return constraints.adverse_price(
            price + adjustment if buying else price - adjustment, buying=buying
        )
    if transaction_type is TransactionType.BUY:
        return quantize_money(price + adjustment)
    return quantize_money(price - adjustment)


def _walk_book(levels: Sequence[DepthLevel], quantity: int, lot_size: int) -> list[SimulatedFill]:
    """Consume depth level by level until the quantity is met or the book runs out."""
    remaining = quantity
    fills: list[SimulatedFill] = []
    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity) // lot_size * lot_size
        if take <= 0:
            continue
        fills.append(SimulatedFill(quantity=take, price=level.price))
        remaining -= take
    return fills
