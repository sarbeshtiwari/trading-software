"""Market-data transfer objects — MD-002, MD-011, OC-001, GRK-004.

Every object carries ``data_origin`` and the timestamp it was observed at, so
staleness (MD-008) and provenance (ARCH-008) are decidable at any point in the
pipeline rather than assumed.

Optional fields are ``None`` when unknown — never zero. In this domain zero is a
meaningful value (an option with zero OI, a stock with zero volume), and
substituting it for "unknown" produces confident wrong answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, GreekSource, OptionType, Segment

__all__ = [
    "DepthLevel",
    "Quote",
    "LTPQuote",
    "OHLCQuote",
    "Bar",
    "IndexValue",
    "Greeks",
    "OptionLeg",
    "OptionStrike",
    "OptionChain",
    "InstrumentRef",
]


@dataclass(frozen=True)
class InstrumentRef:
    """Minimal identity of an instrument, as used by data calls."""

    trading_symbol: str
    exchange: Exchange
    segment: Segment

    @property
    def key(self) -> str:
        return f"{self.exchange.value}_{self.segment.value}_{self.trading_symbol}"


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    quantity: int
    orders: Optional[int] = None


@dataclass(frozen=True)
class Quote:
    """Full quote including the depth ladder (MD-002)."""

    instrument: InstrumentRef
    ltp: Decimal
    observed_at: datetime
    data_origin: DataOrigin = DataOrigin.LIVE

    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    volume: Optional[int] = None
    average_price: Optional[Decimal] = None

    day_change: Optional[Decimal] = None
    day_change_pct: Optional[Decimal] = None

    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()

    open_interest: Optional[int] = None
    open_interest_change: Optional[int] = None
    implied_volatility: Optional[Decimal] = None

    upper_circuit: Optional[Decimal] = None
    lower_circuit: Optional[Decimal] = None
    week52_high: Optional[Decimal] = None
    week52_low: Optional[Decimal] = None
    market_cap: Optional[Decimal] = None

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def is_crossed(self) -> bool:
        """A crossed book (bid above ask) means the quote cannot be trusted."""
        if self.best_bid is None or self.best_ask is None:
            return False
        return self.best_bid > self.best_ask


@dataclass(frozen=True)
class LTPQuote:
    instrument: InstrumentRef
    ltp: Decimal
    observed_at: datetime
    data_origin: DataOrigin = DataOrigin.LIVE


@dataclass(frozen=True)
class OHLCQuote:
    """Current-session OHLC snapshot — not an interval candle."""

    instrument: InstrumentRef
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    observed_at: datetime
    previous_close: Optional[Decimal] = None
    data_origin: DataOrigin = DataOrigin.LIVE


@dataclass(frozen=True)
class Bar:
    """One OHLCV(+OI) candle."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    open_interest: Optional[int] = None

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_valid(self) -> bool:
        return (
            self.high >= self.low
            and self.high >= self.open
            and self.high >= self.close
            and self.low <= self.open
            and self.low <= self.close
            and self.volume >= 0
        )


@dataclass(frozen=True)
class IndexValue:
    name: str
    value: Decimal
    observed_at: datetime
    change: Optional[Decimal] = None
    change_pct: Optional[Decimal] = None
    data_origin: DataOrigin = DataOrigin.LIVE


@dataclass(frozen=True)
class Greeks:
    """Option risk sensitivities. ``source`` distinguishes broker from computed."""

    delta: Optional[Decimal] = None
    gamma: Optional[Decimal] = None
    theta: Optional[Decimal] = None
    vega: Optional[Decimal] = None
    rho: Optional[Decimal] = None
    implied_volatility: Optional[Decimal] = None
    source: GreekSource = GreekSource.COMPUTED
    computed_at: Optional[datetime] = None
    #: Rate/dividend assumptions used, when computed locally (GRK-005).
    assumptions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionLeg:
    """One side (CE or PE) of a strike."""

    trading_symbol: str
    option_type: OptionType
    ltp: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    open_interest_change: Optional[int] = None
    greeks: Optional[Greeks] = None


@dataclass(frozen=True)
class OptionStrike:
    strike: Decimal
    call: Optional[OptionLeg] = None
    put: Optional[OptionLeg] = None


@dataclass(frozen=True)
class OptionChain:
    """Full strike ladder for one underlying and expiry (OC-001)."""

    underlying: str
    expiry: Optional[date]
    observed_at: datetime
    spot: Optional[Decimal] = None
    strikes: Sequence[OptionStrike] = ()
    data_origin: DataOrigin = DataOrigin.LIVE
    raw: dict[str, Any] = field(default_factory=dict)

    def atm_strike(self) -> Optional[Decimal]:
        """Strike nearest the spot. ``None`` when spot or strikes are missing."""
        if self.spot is None or not self.strikes:
            return None
        return min((s.strike for s in self.strikes), key=lambda k: abs(k - self.spot))
