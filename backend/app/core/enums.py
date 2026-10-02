"""Trading-domain enumerations.

These are the vocabulary of the whole system. Values that correspond to Groww API
fields use **exactly** the strings Groww uses (verified 2026-09-17), so the broker
adapter never needs a translation table for them — a mismatch here would surface
as a rejected order, so they are pinned and tested.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "Exchange",
    "Segment",
    "Product",
    "OrderType",
    "TransactionType",
    "Validity",
    "OrderStatus",
    "InstrumentType",
    "OptionType",
    "PositionSide",
    "PositionState",
    "ExitReason",
    "SignalDirection",
    "MarketRegime",
    "SessionPhase",
    "HealthStatus",
    "Severity",
    "VerificationStatus",
    "GreekSource",
    "MarginEstimateSource",
]


class Exchange(str, Enum):
    """Groww order endpoints document NSE; BSE/MCX stay behind a capability flag."""

    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"


class Segment(str, Enum):
    CASH = "CASH"
    FNO = "FNO"


class Product(str, Enum):
    #: Delivery / cash-and-carry.
    CNC = "CNC"
    #: Intraday margin. Auto-squared-off by the broker; we square off earlier.
    MIS = "MIS"
    #: Carry-forward derivatives.
    NRML = "NRML"

    @property
    def is_intraday(self) -> bool:
        return self is Product.MIS


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "STOP_LOSS_MARKET"

    @property
    def needs_price(self) -> bool:
        return self in (OrderType.LIMIT, OrderType.STOP_LOSS)

    @property
    def needs_trigger_price(self) -> bool:
        return self in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET)


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Used for signed quantity arithmetic."""
        return 1 if self is TransactionType.BUY else -1

    @property
    def opposite(self) -> "TransactionType":
        return TransactionType.SELL if self is TransactionType.BUY else TransactionType.BUY


class Validity(str, Enum):
    """Groww supports DAY only. Anything else must be rejected before submission."""

    DAY = "DAY"


class OrderStatus(str, Enum):
    # Local-only states, before the broker has seen the order.
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    # Groww-reported states.
    OPEN = "OPEN"
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    # Local states for partial progress and lost visibility.
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    @property
    def is_open_at_broker(self) -> bool:
        return self in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"

    @property
    def is_derivative(self) -> bool:
        return self in (InstrumentType.FUTURE, InstrumentType.OPTION)


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class PositionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    #: Found at the broker with no local record; adopted and flagged (REC-005).
    ADOPTED = "ADOPTED"


class ExitReason(str, Enum):
    """Why a position was closed. An enum, never free text (JRN-003)."""

    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    INVALIDATION = "INVALIDATION"
    SQUARE_OFF = "SQUARE_OFF"
    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"
    RISK_REDUCTION = "RISK_REDUCTION"
    EXPIRY = "EXPIRY"


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"
    NONE = "NONE"


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    EVENT_RISK = "EVENT_RISK"
    UNKNOWN = "UNKNOWN"


class SessionPhase(str, Enum):
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    REGULAR = "REGULAR"
    CLOSING = "CLOSING"
    POST_MARKET = "POST_MARKET"

    @property
    def allows_orders(self) -> bool:
        return self is SessionPhase.REGULAR


class HealthStatus(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class VerificationStatus(str, Enum):
    """News verification outcome (NEWS-005)."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"


class GreekSource(str, Enum):
    BROKER = "BROKER"
    COMPUTED = "COMPUTED"


class MarginEstimateSource(str, Enum):
    BROKER = "BROKER"
    ESTIMATED = "ESTIMATED"
