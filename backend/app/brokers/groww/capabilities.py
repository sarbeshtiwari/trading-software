"""Groww capability declaration — GRW-025.

Declaring what the broker supports lets higher layers fail *locally* with a clear
message instead of discovering a limitation as a rejected order. The two that
bite hardest:

* **Validity is ``DAY`` only.** No IOC, and no GTT — so a stop cannot be parked
  overnight at the broker. Protection has to be re-placed each session and
  monitored locally (EXEC-003).
* **No bracket or cover orders.** Entry and stop are separate orders, which is
  why the executor has an explicit unprotected-position escalation path.

Values verified against the official documentation on 2026-09-17.
"""

from __future__ import annotations

from app.brokers.models import BrokerCapabilities
from app.core.enums import Exchange, OrderType, Product, Segment, Validity

__all__ = ["GROWW_CAPABILITIES", "assert_supported"]

GROWW_CAPABILITIES = BrokerCapabilities(
    name="groww",
    # The order documentation lists NSE. BSE appears in the SDK exchange
    # constants but is not confirmed on the order path, so it stays out until a
    # live account confirms it.
    supported_exchanges=frozenset({Exchange.NSE}),
    supported_segments=frozenset({Segment.CASH, Segment.FNO}),
    supported_products=frozenset({Product.CNC, Product.MIS, Product.NRML}),
    supported_order_types=frozenset(
        {
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.STOP_LOSS,
            OrderType.STOP_LOSS_MARKET,
        }
    ),
    supported_validities=frozenset({Validity.DAY}),
    supports_bracket_orders=False,
    supports_gtt=False,
    supports_amo=True,
    max_batch_quote_symbols=50,
    max_feed_subscriptions=1000,
    max_orders_per_second=10,
    max_orders_per_minute=250,
)


def assert_supported(
    *,
    exchange: Exchange,
    segment: Segment,
    product: Product,
    order_type: OrderType,
    validity: Validity,
) -> None:
    """Raise a local validation error for anything Groww cannot accept."""
    from app.core.errors import ValidationError

    caps = GROWW_CAPABILITIES
    problems: list[str] = []

    if exchange not in caps.supported_exchanges:
        problems.append(
            f"exchange {exchange.value} is not supported on the order path "
            f"(supported: {sorted(e.value for e in caps.supported_exchanges)})"
        )
    if segment not in caps.supported_segments:
        problems.append(f"segment {segment.value} is not supported")
    if product not in caps.supported_products:
        problems.append(f"product {product.value} is not supported")
    if order_type not in caps.supported_order_types:
        problems.append(f"order type {order_type.value} is not supported")
    if validity not in caps.supported_validities:
        problems.append(
            f"validity {validity.value} is not supported; Groww accepts DAY only"
        )

    if problems:
        raise ValidationError(
            "Groww cannot accept this order: " + "; ".join(problems),
            context={"problems": problems},
        )
