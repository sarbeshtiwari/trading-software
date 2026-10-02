"""MarketDataProvider interface — ARCH-005.

One abstraction over "where do prices come from", implemented by the live
provider (broker feed + REST), the historical provider (local store + broker
history) and the replay provider (stored bars fed in order, no look-ahead).

Batch methods take and return sequences because the underlying API batches: LTP
and OHLC accept up to 50 instruments per call. Callers pass whatever they need
and the adapter chunks; a per-symbol loop would burn the rate limit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Awaitable, Callable, Optional, Sequence

from app.core.data_origin import DataOrigin
from app.marketdata.models import (
    Bar,
    IndexValue,
    InstrumentRef,
    LTPQuote,
    OHLCQuote,
    OptionChain,
    Quote,
)

__all__ = ["MarketDataProvider", "TickCallback"]

TickCallback = Callable[[LTPQuote], Awaitable[None]]


class MarketDataProvider(ABC):
    """Everything the system may ask for market data."""

    name: str = "abstract"

    @property
    @abstractmethod
    def data_origin(self) -> DataOrigin:
        """Provenance stamped on everything this provider returns."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # --- Snapshots --------------------------------------------------------

    @abstractmethod
    async def get_quote(self, instrument: InstrumentRef) -> Quote:
        """Full quote with depth, OI and circuit limits."""

    @abstractmethod
    async def get_ltp(self, instruments: Sequence[InstrumentRef]) -> dict[str, LTPQuote]:
        """Last traded price for many instruments, keyed by ``InstrumentRef.key``.

        Implementations must chunk to the broker's batch limit transparently.
        """

    @abstractmethod
    async def get_ohlc(self, instruments: Sequence[InstrumentRef]) -> dict[str, OHLCQuote]:
        """Session OHLC for many instruments, keyed by ``InstrumentRef.key``."""

    @abstractmethod
    async def get_index_value(self, name: str) -> IndexValue:
        """Current level of an index (NIFTY, BANKNIFTY, FINNIFTY, SENSEX)."""

    # --- History ----------------------------------------------------------

    @abstractmethod
    async def get_candles(
        self,
        instrument: InstrumentRef,
        interval_minutes: int,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        """Candles in ascending time order.

        Implementations must respect the per-interval maximum range by splitting
        a long request into compliant windows and stitching the results without
        gaps or duplicates (GRW-018).
        """

    # --- Derivatives ------------------------------------------------------

    @abstractmethod
    async def get_option_chain(
        self, underlying: str, expiry: Optional[date] = None
    ) -> OptionChain:
        """Full strike ladder with OI, volume and Greeks where available."""

    # --- Streaming --------------------------------------------------------

    @abstractmethod
    async def subscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        """Add instruments to the live feed, within the subscription budget."""

    @abstractmethod
    async def unsubscribe(self, instruments: Sequence[InstrumentRef]) -> None:
        """Remove instruments from the live feed."""

    @abstractmethod
    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback invoked for each tick."""

    # --- Freshness --------------------------------------------------------

    @abstractmethod
    async def last_update_at(self, instrument: InstrumentRef) -> Optional[datetime]:
        """When this instrument last produced data. Drives staleness (MD-008)."""
