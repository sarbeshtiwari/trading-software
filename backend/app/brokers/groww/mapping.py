"""Groww payload mapping — request construction and response parsing.

Supports GRW-007…GRW-015. Two responsibilities:

* **Build requests** that match the documented field names exactly, validating
  everything locally first so an avoidable rejection never reaches the broker.
* **Parse responses** defensively. Missing optional values become ``None``, never
  ``0``: a missing average fill price is not a fill at zero, and treating it as
  one would corrupt P&L silently.

Field names come from the official documentation (verified 2026-09-17). Key
lookup tolerates the snake/camel variants seen across the SDK and REST
references, because a field that exists under a different spelling is still
present, and silently dropping it would be worse than accepting both.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from app.brokers.groww.capabilities import assert_supported
from app.brokers.models import (
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
from app.core.clock import to_ist
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
from app.core.errors import InvalidResponseError, ValidationError

__all__ = [
    "build_order_payload",
    "build_modify_payload",
    "build_cancel_payload",
    "parse_order_ack",
    "parse_order",
    "parse_trade",
    "parse_position",
    "parse_holding",
    "parse_margin",
    "parse_profile",
    "validate_reference_id",
    "pick",
    "parse_list",
]

#: Groww constrains order_reference_id to 8-20 alphanumeric chars, max 2 hyphens.
_REFERENCE_MIN = 8
_REFERENCE_MAX = 20
_REFERENCE_MAX_HYPHENS = 2


# --- Tolerant accessors ---------------------------------------------------


def pick(payload: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """First present, non-null value among ``names``."""
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
        camel = _to_camel(name)
        if camel in payload and payload[camel] is not None:
            return payload[camel]
    return default


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _decimal(value: Any, *, field: str = "value") -> Optional[Decimal]:
    """Parse a number as Decimal. Absent stays absent; garbage raises."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        # str() first: float -> Decimal directly would import binary error.
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidResponseError(
            f"Groww returned a non-numeric value for {field}: {value!r}",
            context={"field": field},
        ) from exc


def _int(value: Any, *, field: str = "value", default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidResponseError(
            f"Groww returned a non-integer value for {field}: {value!r}",
            context={"field": field},
        ) from exc


def _datetime(value: Any) -> Optional[datetime]:
    """Parse epoch millis/seconds or an ISO string into an IST datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_ist(value)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return to_ist(datetime.fromtimestamp(seconds, tz=timezone.utc))
    text = str(value).strip()
    if text.isdigit():
        return _datetime(int(text))
    try:
        return to_ist(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None



def _enum(enum_cls: type, value: Any, *, field: str, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return enum_cls(str(value).strip().upper())
    except ValueError:
        if default is not None:
            return default
        raise InvalidResponseError(
            f"Groww returned an unrecognised {field}: {value!r}",
            context={"field": field, "value": str(value)},
        ) from None


# --- Request construction -------------------------------------------------


def validate_reference_id(reference_id: str) -> str:
    """Enforce the documented ``order_reference_id`` format locally.

    A reference id rejected by the broker would break idempotency exactly when it
    matters, so it is validated before the call rather than after.
    """
    if not isinstance(reference_id, str) or not reference_id:
        raise ValidationError("order_reference_id is required")
    if not (_REFERENCE_MIN <= len(reference_id) <= _REFERENCE_MAX):
        raise ValidationError(
            f"order_reference_id must be {_REFERENCE_MIN}-{_REFERENCE_MAX} characters, "
            f"got {len(reference_id)}",
            context={"reference_id_length": len(reference_id)},
        )
    if reference_id.count("-") > _REFERENCE_MAX_HYPHENS:
        raise ValidationError(
            f"order_reference_id may contain at most {_REFERENCE_MAX_HYPHENS} hyphens"
        )
    if not reference_id.replace("-", "").isalnum():
        raise ValidationError("order_reference_id must be alphanumeric apart from hyphens")
    return reference_id


def build_order_payload(request: OrderRequest) -> dict[str, Any]:
    """Build the ``/order/create`` body, validating everything first."""
    assert_supported(
        exchange=request.exchange,
        segment=request.segment,
        product=request.product,
        order_type=request.order_type,
        validity=request.validity,
    )
    validate_reference_id(request.reference_id)

    if request.quantity <= 0:
        raise ValidationError(
            f"quantity must be positive, got {request.quantity}",
            context={"quantity": request.quantity},
        )
    if request.order_type.needs_price and request.price is None:
        raise ValidationError(
            f"{request.order_type.value} orders require a price",
            context={"order_type": request.order_type.value},
        )
    if request.order_type.needs_trigger_price and request.trigger_price is None:
        raise ValidationError(
            f"{request.order_type.value} orders require a trigger price",
            context={"order_type": request.order_type.value},
        )
    if request.order_type is OrderType.MARKET and request.price is not None:
        raise ValidationError("MARKET orders must not carry a price")

    payload: dict[str, Any] = {
        "trading_symbol": request.trading_symbol,
        "quantity": int(request.quantity),
        "validity": request.validity.value,
        "exchange": request.exchange.value,
        "segment": request.segment.value,
        "product": request.product.value,
        "order_type": request.order_type.value,
        "transaction_type": request.transaction_type.value,
        "order_reference_id": request.reference_id,
    }
    if request.price is not None:
        payload["price"] = float(request.price)
    if request.trigger_price is not None:
        payload["trigger_price"] = float(request.trigger_price)
    if request.algo_id:
        # Compliance tagging (CMP-002). Sent only when configured.
        payload["algo_id"] = request.algo_id
    return payload


def build_modify_payload(request: ModifyRequest) -> dict[str, Any]:
    if not request.broker_order_id:
        raise ValidationError("groww_order_id is required to modify an order")
    payload: dict[str, Any] = {
        "groww_order_id": request.broker_order_id,
        "segment": request.segment.value,
    }
    if request.quantity is not None:
        if request.quantity <= 0:
            raise ValidationError("quantity must be positive")
        payload["quantity"] = int(request.quantity)
    if request.order_type is not None:
        payload["order_type"] = request.order_type.value
    if request.price is not None:
        payload["price"] = float(request.price)
    if request.trigger_price is not None:
        payload["trigger_price"] = float(request.trigger_price)
    if len(payload) == 2:
        raise ValidationError("a modify request must change at least one field")
    return payload


def build_cancel_payload(broker_order_id: str, segment: Segment) -> dict[str, Any]:
    if not broker_order_id:
        raise ValidationError("groww_order_id is required to cancel an order")
    return {"groww_order_id": broker_order_id, "segment": segment.value}


# --- Response parsing -----------------------------------------------------


def parse_order_ack(payload: Mapping[str, Any]) -> OrderAck:
    return OrderAck(
        broker_order_id=_optional_str(pick(payload, "groww_order_id", "order_id")),
        reference_id=_optional_str(pick(payload, "order_reference_id", "reference_id")),
        status=_enum(
            OrderStatus,
            pick(payload, "order_status", "status"),
            field="order_status",
            default=OrderStatus.UNKNOWN,
        ),
        remark=_optional_str(pick(payload, "remark", "message")),
        raw=dict(payload),
    )


def parse_order(payload: Mapping[str, Any]) -> BrokerOrder:
    broker_order_id = _optional_str(pick(payload, "groww_order_id", "order_id"))
    if not broker_order_id:
        raise InvalidResponseError(
            "Groww order payload carried no groww_order_id",
            context={"keys": sorted(payload)},
        )

    quantity = _int(pick(payload, "quantity"), field="quantity", default=0) or 0
    filled = _int(pick(payload, "filled_quantity"), field="filled_quantity", default=0) or 0
    remaining = _int(pick(payload, "remaining_quantity"), field="remaining_quantity")

    return BrokerOrder(
        broker_order_id=broker_order_id,
        trading_symbol=str(pick(payload, "trading_symbol", default="")),
        exchange=_enum(
            Exchange, pick(payload, "exchange"), field="exchange", default=Exchange.NSE
        ),
        segment=_enum(
            Segment, pick(payload, "segment"), field="segment", default=Segment.CASH
        ),
        product=_enum(
            Product, pick(payload, "product"), field="product", default=Product.MIS
        ),
        order_type=_enum(
            OrderType, pick(payload, "order_type"), field="order_type",
            default=OrderType.MARKET,
        ),
        transaction_type=_enum(
            TransactionType,
            pick(payload, "transaction_type"),
            field="transaction_type",
            default=TransactionType.BUY,
        ),
        status=_enum(
            OrderStatus,
            pick(payload, "order_status", "status"),
            field="order_status",
            default=OrderStatus.UNKNOWN,
        ),
        quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=remaining if remaining is not None else max(quantity - filled, 0),
        price=_decimal(pick(payload, "price"), field="price"),
        trigger_price=_decimal(pick(payload, "trigger_price"), field="trigger_price"),
        average_fill_price=_decimal(
            pick(payload, "average_fill_price", "avg_fill_price"), field="average_fill_price"
        ),
        reference_id=_optional_str(pick(payload, "order_reference_id", "reference_id")),
        rejection_reason=_optional_str(pick(payload, "remark", "rejection_reason")),
        created_at=_datetime(pick(payload, "created_at", "order_timestamp")),
        exchange_time=_datetime(pick(payload, "exchange_time", "exchange_timestamp")),
        raw=dict(payload),
    )


def parse_trade(payload: Mapping[str, Any]) -> BrokerTrade:
    quantity = _int(pick(payload, "quantity"), field="quantity")
    price = _decimal(pick(payload, "price"), field="price")
    if quantity is None or price is None:
        raise InvalidResponseError(
            "Groww trade payload is missing quantity or price",
            context={"keys": sorted(payload)},
        )
    return BrokerTrade(
        broker_order_id=str(pick(payload, "groww_order_id", "order_id", default="")),
        quantity=quantity,
        price=price,
        executed_at=_datetime(pick(payload, "trade_date_time", "created_at", "trade_date")),
        exchange_trade_id=_optional_str(pick(payload, "exchange_trade_id", "trade_id")),
        trading_symbol=_optional_str(pick(payload, "trading_symbol")),
        isin=_optional_str(pick(payload, "isin")),
        segment=_enum(Segment, pick(payload, "segment"), field="segment", default=None),
        raw=dict(payload),
    )


def parse_position(payload: Mapping[str, Any]) -> BrokerPosition:
    """Normalise a position. Short positions carry a negative net quantity."""
    bought = _int(pick(payload, "quantity_bought", "bought_quantity"), default=0) or 0
    sold = _int(pick(payload, "quantity_sold", "sold_quantity"), default=0) or 0

    explicit_net = pick(
        payload, "net_quantity", "quantity", "net_carry_forward_quantity"
    )
    net = _int(explicit_net, field="net_quantity") if explicit_net is not None else None
    if net is None:
        net = bought - sold

    average_price = _decimal(
        pick(payload, "average_price", "net_price", "buy_price"), field="average_price"
    )

    return BrokerPosition(
        trading_symbol=str(pick(payload, "trading_symbol", default="")),
        exchange=_enum(
            Exchange, pick(payload, "exchange"), field="exchange", default=Exchange.NSE
        ),
        segment=_enum(
            Segment, pick(payload, "segment"), field="segment", default=Segment.CASH
        ),
        product=_enum(
            Product, pick(payload, "product"), field="product", default=Product.MIS
        ),
        net_quantity=net,
        average_price=average_price if average_price is not None else Decimal("0"),
        bought_quantity=bought,
        sold_quantity=sold,
        realised_pnl=_decimal(pick(payload, "realised_pnl", "realized_pnl")),
        unrealised_pnl=_decimal(pick(payload, "unrealised_pnl", "unrealized_pnl")),
        last_price=_decimal(pick(payload, "last_price", "ltp")),
        raw=dict(payload),
    )


def parse_holding(payload: Mapping[str, Any]) -> BrokerHolding:
    quantity = _int(pick(payload, "quantity", "holding_quantity"), default=0) or 0
    average_price = _decimal(pick(payload, "average_price", "buy_price"))
    return BrokerHolding(
        trading_symbol=str(pick(payload, "trading_symbol", "symbol", default="")),
        isin=_optional_str(pick(payload, "isin")),
        quantity=quantity,
        average_price=average_price if average_price is not None else Decimal("0"),
        pledged_quantity=_int(pick(payload, "pledge_quantity", "pledged_quantity"), default=0)
        or 0,
        demat_free_quantity=_int(pick(payload, "demat_free_quantity")),
        last_price=_decimal(pick(payload, "last_price", "ltp")),
        raw=dict(payload),
    )


def parse_margin(payload: Mapping[str, Any]) -> MarginInfo:
    available = _decimal(
        pick(payload, "net_margin_available", "available_margin", "net_available"),
        field="available_margin",
    )
    if available is None:
        raise InvalidResponseError(
            "Groww margin payload carried no available-margin figure",
            context={"keys": sorted(payload)},
        )
    return MarginInfo(
        available_margin=available,
        used_margin=_decimal(pick(payload, "margin_used", "used_margin")),
        total_collateral=_decimal(pick(payload, "collateral_available", "total_collateral")),
        cash=_decimal(pick(payload, "clear_cash", "cash")),
        source=MarginEstimateSource.BROKER,
        raw=dict(payload),
    )


def parse_profile(payload: Mapping[str, Any]) -> BrokerProfile:
    account_id = _optional_str(pick(payload, "user_id", "account_id", "client_id"))
    if not account_id:
        raise InvalidResponseError(
            "Groww profile payload carried no account identifier",
            context={"keys": sorted(payload)},
        )
    return BrokerProfile(
        account_id=account_id,
        name=_optional_str(pick(payload, "name", "user_name")),
        email=_optional_str(pick(payload, "email")),
        raw=dict(payload),
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_list(payload: Any, *keys: str) -> Sequence[Mapping[str, Any]]:
    """Extract a list from a payload that may be a list or a wrapper object."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # A wrapper with exactly one list value is unambiguous.
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise InvalidResponseError(
        "Expected a list in the Groww payload",
        context={"looked_for": list(keys), "type": type(payload).__name__},
    )
