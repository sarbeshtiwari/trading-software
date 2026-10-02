"""Local history archive coverage — HD-010.

Groww serves three months of intraday history. Anything older has to be
accumulated locally, day by day, from the moment the system starts running. That
means early backtests are data-poor and there is no way around it: the honest
response is to measure the archive and say so, loudly, in every report that
depends on it (BT-014).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.brokers.groww.historical import INTRADAY_HISTORY_DAYS
from app.core.clock import Clock, get_clock
from app.core.logging import get_logger
from app.marketdata.ingest import CandleStore
from app.marketdata.models import InstrumentRef

logger = get_logger("marketdata.archive")

__all__ = ["SeriesCoverage", "CoverageReport", "ArchiveReporter"]


@dataclass(frozen=True)
class SeriesCoverage:
    instrument: str
    interval_minutes: int
    earliest: Optional[datetime]
    latest: Optional[datetime]
    bars: int

    @property
    def days_covered(self) -> float:
        if self.earliest is None or self.latest is None:
            return 0.0
        return (self.latest - self.earliest).total_seconds() / 86400.0

    @property
    def exceeds_broker_window(self) -> bool:
        """True once the local archive reaches further back than the broker does."""
        return self.days_covered > INTRADAY_HISTORY_DAYS

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "interval_minutes": self.interval_minutes,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
            "bars": self.bars,
            "days_covered": round(self.days_covered, 2),
            "exceeds_broker_window": self.exceeds_broker_window,
        }


@dataclass
class CoverageReport:
    generated_at: datetime
    series: list[SeriesCoverage] = field(default_factory=list)
    minimum_days: int = 60

    @property
    def shortfalls(self) -> list[SeriesCoverage]:
        return [item for item in self.series if item.days_covered < self.minimum_days]

    def warning(self) -> Optional[str]:
        short = self.shortfalls
        if not short:
            return None
        return (
            f"{len(short)} of {len(self.series)} series have less than "
            f"{self.minimum_days} days of history. Groww serves only "
            f"{INTRADAY_HISTORY_DAYS} days of intraday data, so the local archive is "
            f"still filling. Backtests over these series are not statistically "
            f"meaningful yet."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "minimum_days": self.minimum_days,
            "series": [item.to_dict() for item in self.series],
            "warning": self.warning(),
        }


class ArchiveReporter:
    def __init__(
        self,
        *,
        store: Optional[CandleStore] = None,
        clock: Optional[Clock] = None,
        minimum_days: int = 60,
    ) -> None:
        self._store = store or CandleStore(clock=clock)
        self._clock = clock or get_clock()
        self._minimum_days = minimum_days

    async def report(
        self,
        instruments: Sequence[tuple[str, InstrumentRef]],
        intervals: Sequence[int] = (1, 5, 15),
    ) -> CoverageReport:
        report = CoverageReport(
            generated_at=self._clock.now(), minimum_days=self._minimum_days
        )
        for instrument_id, instrument in instruments:
            for interval in intervals:
                earliest, latest, count = await self._store.coverage(instrument_id, interval)
                report.series.append(
                    SeriesCoverage(
                        instrument=instrument.key,
                        interval_minutes=interval,
                        earliest=earliest,
                        latest=latest,
                        bars=count,
                    )
                )

        warning = report.warning()
        if warning:
            logger.warning("Archive coverage shortfall", extra={"detail": warning})
        return report
