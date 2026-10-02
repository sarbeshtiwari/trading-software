"""Trading calendar — EXCH-001, EXCH-003.

Weekends are structural and computed. Holidays are *data*, loaded from
``data/holidays.json``, because most Indian market holidays follow lunar calendars
and cannot be derived.

The calendar is deliberately loud about incompleteness. A year whose holiday list
is marked incomplete makes :meth:`TradingCalendar.completeness_warning` return a
message, which the health check surfaces — because the failure mode of a missing
holiday is the system believing the market is open, placing orders into a closed
session, and treating the resulting rejections as a broker fault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

from app.core.clock import ensure_ist, get_clock
from app.core.errors import ConfigurationError
from app.core.logging import get_logger

logger = get_logger("core.calendar")

__all__ = ["Holiday", "SpecialSession", "TradingCalendar", "get_trading_calendar"]

#: app/core/calendar.py -> backend/data/holidays.json
DEFAULT_HOLIDAY_FILE = Path(__file__).resolve().parents[2] / "data" / "holidays.json"

#: Saturday and Sunday.
_WEEKEND = {5, 6}


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str


@dataclass(frozen=True)
class SpecialSession:
    """An off-schedule session such as Muhurat trading (EXCH-003)."""

    day: date
    name: str
    start: time
    end: time


class TradingCalendar:
    def __init__(
        self,
        holidays: Iterable[Holiday] = (),
        *,
        special_sessions: Iterable[SpecialSession] = (),
        complete_years: Iterable[int] = (),
        source: str = "",
    ) -> None:
        self._holidays: dict[date, Holiday] = {h.day: h for h in holidays}
        self._special: dict[date, SpecialSession] = {s.day: s for s in special_sessions}
        self._complete_years = set(complete_years)
        self.source = source

    # --- Construction -----------------------------------------------------

    @classmethod
    def from_file(
        cls, path: Optional[Path] = None, *, as_of: Optional[datetime] = None
    ) -> "TradingCalendar":
        resolved = Path(path or DEFAULT_HOLIDAY_FILE)
        if not resolved.exists():
            raise ConfigurationError(
                f"Holiday file not found at {resolved}. The trading calendar cannot be "
                f"derived; supply the file from the exchange circular.",
                context={"path": str(resolved)},
            )
        data = json.loads(resolved.read_text(encoding="utf-8"))
        cutoff = as_of or get_clock().now()
        if cutoff.tzinfo is None:
            raise ConfigurationError("Calendar as_of must be timezone-aware")

        def available(row):
            published = row.get("available_at")
            if published is None:
                return True
            moment = datetime.fromisoformat(published)
            if moment.tzinfo is None:
                raise ConfigurationError("Calendar availability must be timezone-aware")
            return moment <= cutoff

        holidays: list[Holiday] = []
        specials: list[SpecialSession] = []
        complete: list[int] = []

        for year_text, year_data in (data.get("years") or {}).items():
            year = int(year_text)
            if year_data.get("complete"):
                complete.append(year)
            for row in year_data.get("holidays", []):
                if not available(row):
                    continue
                holidays.append(Holiday(day=date.fromisoformat(row["date"]), name=row["name"]))
            for row in year_data.get("special_sessions", []):
                if not available(row):
                    continue
                specials.append(
                    SpecialSession(
                        day=date.fromisoformat(row["date"]),
                        name=row["name"],
                        start=time.fromisoformat(row["start"]),
                        end=time.fromisoformat(row["end"]),
                    )
                )

        return cls(
            holidays,
            special_sessions=specials,
            complete_years=complete,
            source=str(data.get("source", "")),
        )

    # --- Queries ----------------------------------------------------------

    def is_weekend(self, day: date) -> bool:
        return day.weekday() in _WEEKEND

    def is_holiday(self, day: date) -> bool:
        return day in self._holidays

    def holiday_name(self, day: date) -> Optional[str]:
        holiday = self._holidays.get(day)
        return holiday.name if holiday else None

    def special_session(self, day: date) -> Optional[SpecialSession]:
        return self._special.get(day)

    def is_trading_day(self, day: date) -> bool:
        """A special session makes an otherwise closed day a trading day."""
        if self.special_session(day) is not None:
            return True
        return not self.is_weekend(day) and not self.is_holiday(day)

    def next_trading_day(self, day: date, *, inclusive: bool = False) -> date:
        candidate = day if inclusive else day + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise ConfigurationError(
            f"No trading day found within 30 days of {day}; the holiday data looks wrong",
            context={"from": day.isoformat()},
        )

    def previous_trading_day(self, day: date, *, inclusive: bool = False) -> date:
        candidate = day if inclusive else day - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise ConfigurationError(
            f"No trading day found within 30 days before {day}",
            context={"from": day.isoformat()},
        )

    def trading_days_between(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        candidate = start
        while candidate <= end:
            if self.is_trading_day(candidate):
                days.append(candidate)
            candidate += timedelta(days=1)
        return days

    def sessions_between(self, start: datetime, end: datetime) -> int:
        """Trading days in a datetime range — used for time-to-expiry (GRK-007)."""
        return len(self.trading_days_between(ensure_ist(start).date(), ensure_ist(end).date()))

    @property
    def holiday_count(self) -> int:
        return len(self._holidays)

    # --- Integrity --------------------------------------------------------

    def is_year_complete(self, year: int) -> bool:
        return year in self._complete_years

    def completeness_warning(self, year: int) -> Optional[str]:
        """Message for the health check when a year is not fully populated."""
        if self.is_year_complete(year):
            return None
        known = sum(1 for day in self._holidays if day.year == year)
        return (
            f"Holiday data for {year} is marked incomplete ({known} dates known). "
            f"Exchange/segment coverage or special-session timings remain unverified. "
            f"Verify data/holidays.json against {self.source or 'the exchange circular'} "
            f"before marking the calendar complete."
        )


_calendar: Optional[TradingCalendar] = None


def get_trading_calendar(*, reload: bool = False) -> TradingCalendar:
    global _calendar
    if _calendar is None or reload:
        _calendar = TradingCalendar.from_file()
        logger.info(
            "Loaded trading calendar",
            extra={"source": _calendar.source, "holidays": _calendar.holiday_count},
        )
    return _calendar
