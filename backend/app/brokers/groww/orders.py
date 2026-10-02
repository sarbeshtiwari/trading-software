"""Groww order operations — GRW-007…GRW-012, and the guard for GRW-006.

The single most important line in this module is ``allow_retry=False`` on order
creation. A timed-out ``/order/create`` may still have reached the exchange;
resending it is how a system ends up with two positions where it planned one. The
executor resolves a timeout by looking the order up by its reference id
(:meth:`GrowwOrdersApi.get_by_reference`) before deciding anything.

Pagination limits are the documented ones: order list at most 100 per page,
trades at most 50.
"""

from __future__ import annotations

from typing import Any, Optional

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.errors import GrowwError, GrowwNotFoundError
from app.brokers.groww.mapping import (
    build_cancel_payload,
    build_modify_payload,
    build_order_payload,
    parse_list,
    parse_order,
    parse_order_ack,
    parse_trade,
)
from app.brokers.groww.ratelimit import RateCategory
from app.brokers.models import BrokerOrder, BrokerTrade, ModifyRequest, OrderAck, OrderRequest
from app.core.enums import OrderStatus, Segment
from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger("brokers.groww.orders")

__all__ = ["GrowwOrdersApi", "ORDER_LIST_PAGE_SIZE", "TRADE_LIST_PAGE_SIZE"]

ORDER_LIST_PAGE_SIZE = 100
TRADE_LIST_PAGE_SIZE = 50
#: Guards against an unbounded pagination loop on a misbehaving response.
_MAX_PAGES = 50


class GrowwOrdersApi:
    def __init__(self, client: GrowwClient) -> None:
        self._client = client

    # --- Create -----------------------------------------------------------

    async def place(self, request: OrderRequest) -> OrderAck:
        """Submit an order. **Never retried** — see EXEC-005."""
        payload = build_order_payload(request)
        logger.info(
            "Placing order",
            extra={
                "trading_symbol": request.trading_symbol,
                "transaction_type": request.transaction_type.value,
                "quantity": request.quantity,
                "order_type": request.order_type.value,
                "segment": request.segment.value,
                "reference_id": request.reference_id,
            },
        )
        result = await self._client.post(
            Endpoints.ORDER_CREATE.resolve(),
            category=RateCategory.ORDERS,
            json=payload,
            allow_retry=False,
        )
        return parse_order_ack(_as_mapping(result))

    # --- Modify / cancel --------------------------------------------------

    async def modify(self, request: ModifyRequest) -> OrderAck:
        """Modify a pending or open order.

        A modify against a terminal order is refused locally: the broker would
        reject it anyway, and spending an order-rate token to learn that is waste.
        """
        existing = await self.get(request.broker_order_id, request.segment)
        if existing.status.is_terminal:
            raise ValidationError(
                f"Order {request.broker_order_id} is already {existing.status.value}; "
                f"it cannot be modified",
                context={"status": existing.status.value},
            )

        payload = build_modify_payload(request)
        result = await self._client.post(
            Endpoints.ORDER_MODIFY.resolve(),
            category=RateCategory.ORDERS,
            json=payload,
            allow_retry=False,
        )
        return parse_order_ack(_as_mapping(result))

    async def cancel(self, broker_order_id: str, segment: Segment) -> OrderAck:
        """Cancel an order. Cancelling an already-cancelled order succeeds.

        Idempotence matters here because cancellation is what emergency controls
        call, often repeatedly, and an exception mid-sweep would strand the rest.
        """
        payload = build_cancel_payload(broker_order_id, segment)
        try:
            result = await self._client.post(
                Endpoints.ORDER_CANCEL.resolve(),
                category=RateCategory.ORDERS,
                json=payload,
                allow_retry=False,
            )
        except GrowwError as exc:
            current = await self._safe_status(broker_order_id, segment)
            if current is not None and current.status is OrderStatus.CANCELLED:
                logger.info(
                    "Cancel reported an error but the order is already cancelled",
                    extra={"groww_order_id": broker_order_id, "broker_code": exc.broker_code},
                )
                return OrderAck(
                    broker_order_id=broker_order_id,
                    reference_id=current.reference_id,
                    status=OrderStatus.CANCELLED,
                    remark="already cancelled",
                    raw=dict(current.raw),
                )
            raise
        return parse_order_ack(_as_mapping(result))

    # --- Read -------------------------------------------------------------

    async def get(self, broker_order_id: str, segment: Segment) -> BrokerOrder:
        result = await self._client.get(
            Endpoints.ORDER_DETAIL.resolve(groww_order_id=broker_order_id),
            category=RateCategory.NON_TRADING,
            params={"segment": segment.value},
        )
        return parse_order(_as_mapping(result))

    async def get_by_reference(
        self, reference_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        """Look an order up by our idempotency key.

        Returns ``None`` when the broker has no such order — which is the signal
        that a timed-out submission did *not* reach the exchange and may be
        retried deliberately by the caller.
        """
        try:
            result = await self._client.get(
                Endpoints.ORDER_STATUS_BY_REFERENCE.resolve(order_reference_id=reference_id),
                category=RateCategory.NON_TRADING,
                params={"segment": segment.value},
            )
        except GrowwNotFoundError:
            return None
        payload = _as_mapping(result)
        if not payload:
            return None
        return parse_order(payload)

    async def list(self, segment: Optional[Segment] = None) -> list[BrokerOrder]:
        """Every order from today, across pages."""
        orders: list[BrokerOrder] = []
        seen: set[str] = set()

        for page in range(_MAX_PAGES):
            params: dict[str, Any] = {"page": page, "page_size": ORDER_LIST_PAGE_SIZE}
            if segment is not None:
                params["segment"] = segment.value

            result = await self._client.get(
                Endpoints.ORDER_LIST.resolve(),
                category=RateCategory.NON_TRADING,
                params=params,
            )
            rows = parse_list(result, "order_list", "orders")
            if not rows:
                break

            for row in rows:
                order = parse_order(row)
                if order.broker_order_id not in seen:
                    seen.add(order.broker_order_id)
                    orders.append(order)

            if len(rows) < ORDER_LIST_PAGE_SIZE:
                break
        else:  # pragma: no cover - only on a pathological response
            logger.error(
                "Order list pagination hit the page ceiling; results may be truncated",
                extra={"max_pages": _MAX_PAGES},
            )

        return orders

    async def trades(self, broker_order_id: str, segment: Segment) -> list[BrokerTrade]:
        """Fills for one order, across pages."""
        trades: list[BrokerTrade] = []

        for page in range(_MAX_PAGES):
            result = await self._client.get(
                Endpoints.ORDER_TRADES.resolve(groww_order_id=broker_order_id),
                category=RateCategory.NON_TRADING,
                params={
                    "segment": segment.value,
                    "page": page,
                    "page_size": TRADE_LIST_PAGE_SIZE,
                },
            )
            rows = parse_list(result, "trade_list", "trades")
            if not rows:
                break
            trades.extend(parse_trade(row) for row in rows)
            if len(rows) < TRADE_LIST_PAGE_SIZE:
                break

        return trades

    # --- Helpers ----------------------------------------------------------

    async def _safe_status(
        self, broker_order_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        """Best-effort status lookup that never masks the original error."""
        try:
            return await self.get(broker_order_id, segment)
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            logger.debug(
                "Status lookup during cancel failed",
                extra={"groww_order_id": broker_order_id, "error": str(exc)},
            )
            return None


def _as_mapping(result: Any) -> dict[str, Any]:
    """Coerce a payload to a mapping, tolerating a single-item list wrapper."""
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        return result[0]
    if result in (None, [], {}):
        return {}
    from app.core.errors import InvalidResponseError

    raise InvalidResponseError(
        f"Expected an object payload, got {type(result).__name__}",
        context={"type": type(result).__name__},
    )
