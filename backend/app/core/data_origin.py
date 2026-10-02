"""Data provenance tagging — ARCH-008, MD-011, PAPER-007.

Every piece of market data, every order, trade, position and P&L figure carries
a provenance tag. Two independent facts are tracked:

* :class:`DataOrigin` — where the *data* came from.
* :class:`ExecutionRealism` — whether an *execution* actually happened at a broker.

Keeping them separate matters: paper trading runs on ``LIVE`` market data with
``SIMULATED`` executions, and a backtest runs on ``HISTORICAL`` data with
``SIMULATED`` executions. Collapsing them into one flag loses the distinction
that makes paper results meaningful.

The guard in :func:`assert_safe_for_execution` is the mechanical enforcement of
"demo data must never reach LIVE".
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from app.core.errors import SyntheticDataInLiveError
from app.modes import TradingMode

__all__ = [
    "DataOrigin",
    "ExecutionRealism",
    "HasDataOrigin",
    "assert_safe_for_execution",
    "is_real_market_data",
]


class DataOrigin(str, Enum):
    """Where a datum came from."""

    #: Real-time data from the broker/exchange feed, right now.
    LIVE = "LIVE"
    #: Real data recorded in the past, read from the local store or the broker.
    HISTORICAL = "HISTORICAL"
    #: Historical data being replayed through the engine as if it were live.
    REPLAY = "REPLAY"
    #: Generated data. Test fixtures and simulators only.
    SYNTHETIC = "SYNTHETIC"

    @property
    def is_real(self) -> bool:
        """True when the data reflects prices that actually traded."""
        return self in (DataOrigin.LIVE, DataOrigin.HISTORICAL)


class ExecutionRealism(str, Enum):
    """Whether an execution actually occurred at a broker."""

    #: Sent to a real broker and acknowledged.
    REAL = "REAL"
    #: Produced by the paper fill engine or the backtester.
    SIMULATED = "SIMULATED"


@runtime_checkable
class HasDataOrigin(Protocol):
    """Anything carrying provenance."""

    data_origin: DataOrigin


def is_real_market_data(origin: DataOrigin) -> bool:
    return origin.is_real


def assert_safe_for_execution(
    origin: DataOrigin,
    mode: TradingMode,
    *,
    what: str = "market data",
    detail: Optional[str] = None,
) -> None:
    """Raise if data of this origin must not drive execution in this mode.

    ARCH-008. In ``LIVE`` only genuinely live data may reach an execution path:

    * ``SYNTHETIC`` is never acceptable — it is invented.
    * ``REPLAY`` is never acceptable — it is the past pretending to be now.
    * ``HISTORICAL`` is rejected on the execution path because an order priced
      off a stale historical bar is an order priced off the wrong price. (Reading
      history for *analysis* is fine; this guard sits on execution only.)

    ``PAPER`` and ``SUPERVISED`` are permissive here: paper exists to run
    simulations, and ``SUPERVISED`` still has a human gate in front of every
    order. The prohibition is specifically about autonomous real money.
    """
    if mode is not TradingMode.LIVE:
        return
    if origin is DataOrigin.LIVE:
        return
    raise SyntheticDataInLiveError(
        f"Refusing to execute in LIVE mode using {what} with origin {origin.value}. "
        f"Only DataOrigin.LIVE may drive execution in LIVE mode."
        + (f" {detail}" if detail else ""),
        context={"origin": origin.value, "mode": mode.value, "what": what},
    )
