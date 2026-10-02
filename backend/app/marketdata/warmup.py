"""Indicator warm-up — HD-007.

An indicator with a 200-period lookback fed 150 bars does not fail; it returns a
number. That number is wrong, and nothing downstream can tell. So the history
requirement is checked *before* a strategy is allowed to emit anything, and a
shortfall raises rather than degrading quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from app.core.errors import InsufficientHistoryError
from app.core.logging import get_logger
from app.marketdata.ingest import CandleStore
from app.marketdata.models import Bar, InstrumentRef

logger = get_logger("marketdata.warmup")

__all__ = ["WarmupRequirement", "WarmupReport", "check_warmup", "ensure_warmup"]


@dataclass(frozen=True)
class WarmupRequirement:
    interval_minutes: int
    bars: int
    reason: str = ""


@dataclass(frozen=True)
class WarmupReport:
    instrument: str
    interval_minutes: int
    required: int
    available: int

    @property
    def satisfied(self) -> bool:
        return self.available >= self.required

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.available)

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "interval_minutes": self.interval_minutes,
            "required": self.required,
            "available": self.available,
            "shortfall": self.shortfall,
            "satisfied": self.satisfied,
        }


def check_warmup(
    instrument: InstrumentRef,
    requirement: WarmupRequirement,
    bars: Sequence[Bar],
) -> WarmupReport:
    return WarmupReport(
        instrument=instrument.key,
        interval_minutes=requirement.interval_minutes,
        required=requirement.bars,
        available=len(bars),
    )


async def ensure_warmup(
    instrument: InstrumentRef,
    requirement: WarmupRequirement,
    *,
    instrument_id: Optional[str] = None,
    store: Optional[CandleStore] = None,
) -> WarmupReport:
    """Raise :class:`InsufficientHistoryError` when the lookback cannot be met."""
    candle_store = store or CandleStore()
    bars = await candle_store.last_n(
        instrument_id or instrument.key, requirement.interval_minutes, requirement.bars
    )
    report = check_warmup(instrument, requirement, bars)

    if not report.satisfied:
        logger.warning("Warm-up not satisfied", extra=report.to_dict())
        raise InsufficientHistoryError(
            f"{instrument.trading_symbol} has {report.available} bars at "
            f"{requirement.interval_minutes}m but needs {report.required}"
            + (f" ({requirement.reason})" if requirement.reason else "")
            + ". The strategy cannot emit signals until the history is backfilled.",
            context=report.to_dict(),
        )
    return report
