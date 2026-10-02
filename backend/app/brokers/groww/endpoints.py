"""Groww endpoint registry — the single place paths are written down.

Groww publishes the base URL, the envelope, the error codes, the rate limits and
the enums, but its public REST reference spells out only some endpoint paths in
full. Rather than scatter half-known paths through the adapter, every path lives
here with an explicit :class:`Confidence`:

* ``DOCUMENTED`` — the path appears verbatim in the official REST documentation.
* ``INFERRED`` — the operation is documented (it exists in the SDK reference) but
  the exact REST path is not published; this is our best reading of it.

An ``INFERRED`` path logs a warning the first time it is used, and every one of
them is listed in ``docs/LIMITATIONS.md``. They must be confirmed against a live
account before LIVE trading. This is the difference between "unverified and
labelled" and "fabricated", and the distinction matters when real money is on the
other end.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.logging import get_logger

logger = get_logger("brokers.groww.endpoints")

__all__ = ["Confidence", "Endpoint", "Endpoints", "inferred_endpoints"]


class Confidence(str, Enum):
    DOCUMENTED = "documented"
    INFERRED = "inferred"


@dataclass(frozen=True)
class Endpoint:
    path: str
    confidence: Confidence
    note: str = ""

    def resolve(self, **params: object) -> str:
        """Fill path parameters, warning once if the path is only inferred."""
        if self.confidence is Confidence.INFERRED:
            _warn_once(self.path)
        return self.path.format(**params) if params else self.path


_warned: set[str] = set()


def _warn_once(path: str) -> None:
    if path in _warned:
        return
    _warned.add(path)
    logger.warning(
        "Using an inferred Groww endpoint path; confirm it against a live account "
        "before LIVE trading",
        extra={"path": path},
    )


class Endpoints:
    """Every path the adapter uses."""

    # --- Authentication ---------------------------------------------------
    TOKEN = Endpoint("/token/api/access", Confidence.DOCUMENTED)

    # --- Orders -----------------------------------------------------------
    ORDER_CREATE = Endpoint("/order/create", Confidence.DOCUMENTED)
    ORDER_DETAIL = Endpoint("/order/detail/{groww_order_id}", Confidence.DOCUMENTED)
    ORDER_MODIFY = Endpoint("/order/modify", Confidence.INFERRED)
    ORDER_CANCEL = Endpoint("/order/cancel", Confidence.INFERRED)
    ORDER_STATUS = Endpoint("/order/status/{groww_order_id}", Confidence.INFERRED)
    ORDER_STATUS_BY_REFERENCE = Endpoint(
        "/order/status/reference/{order_reference_id}",
        Confidence.INFERRED,
        note="The operation is documented (get_order_status_by_reference); the path is not.",
    )
    ORDER_LIST = Endpoint("/order/list", Confidence.INFERRED)
    ORDER_TRADES = Endpoint("/order/trades/{groww_order_id}", Confidence.INFERRED)

    # --- Portfolio --------------------------------------------------------
    POSITIONS = Endpoint("/positions/user", Confidence.INFERRED)
    POSITIONS_BY_SYMBOL = Endpoint("/positions/symbol", Confidence.INFERRED)
    HOLDINGS = Endpoint("/holdings/user", Confidence.INFERRED)
    MARGIN = Endpoint("/margins/detail/user", Confidence.INFERRED)
    MARGIN_REQUIRED = Endpoint("/margins/detail/orders", Confidence.INFERRED)

    # --- Market data ------------------------------------------------------
    QUOTE = Endpoint("/live-data/quote", Confidence.INFERRED)
    LTP = Endpoint("/live-data/ltp", Confidence.INFERRED)
    OHLC = Endpoint("/live-data/ohlc", Confidence.INFERRED)
    HISTORICAL_CANDLES = Endpoint("/historical/candle/range", Confidence.INFERRED)
    OPTION_CHAIN = Endpoint("/live-data/option-chain", Confidence.INFERRED)
    GREEKS = Endpoint("/live-data/greeks", Confidence.INFERRED)

    # --- Instruments ------------------------------------------------------
    INSTRUMENTS = Endpoint("/instruments", Confidence.INFERRED)
    #: Groww publishes the instrument master as a downloadable CSV.
    INSTRUMENTS_CSV = Endpoint(
        "https://growwapi-assets.groww.in/instruments/instrument.csv",
        Confidence.INFERRED,
        note="Absolute URL; fetched outside the versioned API base.",
    )


def inferred_endpoints() -> tuple[Endpoint, ...]:
    """Every endpoint whose path still needs confirmation against a live account."""
    return tuple(
        value
        for name, value in vars(Endpoints).items()
        if isinstance(value, Endpoint) and value.confidence is Confidence.INFERRED
    )
