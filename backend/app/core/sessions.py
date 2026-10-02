"""Session phases — EXCH-002, EXCH-003.

NSE equity and F&O both run 09:15–15:30 IST, with a pre-open call auction from
09:00 and a post-closing session afterwards. Every time-gated safety behaviour in
the system — entry blackouts, the intraday square-off cutoff, expiry cutoffs,
staleness — is expressed against these phases, so they are computed from an
injected clock and are fully testable at each boundary minute.

Phases are deliberately *not* "open/closed": placing an order at 09:10 is a
different mistake from placing one at 16:10, and the operator should see which.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

from app.core.calendar import TradingCalendar, get_trading_calendar
from app.core.clock import IST, Clock, ensure_ist, get_clock
from app.core.enums import SessionPhase, Segment

__all__ = [
    "SessionWindows",
    "NSE_EQUITY_WINDOWS",
    "phase_at",
    "is_market_open",
    "session_bounds",
    "minutes_until_close",
    "parse_time",
]


@dataclass(frozen=True)
class SessionWindows:
    """Boundaries of one trading day, in IST."""

    pre_open_start: time = time(9, 0)
    regular_start: time = time(9, 15)
    regular_end: time = time(15, 30)
    closing_end: time = time(15, 40)
    post_market_end: time = time(16, 0)


#: NSE equity and derivatives share these windows.
NSE_EQUITY_WINDOWS = SessionWindows()

_WINDOWS_BY_SEGMENT: dict[Segment, SessionWindows] = {
    Segment.CASH: NSE_EQUITY_WINDOWS,
    Segment.FNO: NSE_EQUITY_WINDOWS,
}


def parse_time(value: str) -> time:
    """Parse ``HH:MM`` configuration values into a time."""
    return time.fromisoformat(value if len(value) > 5 else f"{value}:00")


def windows_for(segment: Segment) -> SessionWindows:
    return _WINDOWS_BY_SEGMENT.get(segment, NSE_EQUITY_WINDOWS)


def phase_at(
    moment: Optional[datetime] = None,
    *,
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
    clock: Optional[Clock] = None,
) -> SessionPhase:
    """Which phase the market is in at ``moment``."""
    active_clock = clock or get_clock()
    when = ensure_ist(moment) if moment is not None else active_clock.now()
    cal = calendar or get_trading_calendar()
    day = when.date()

    special = cal.special_session(day)
    if special is not None:
        if special.start <= when.time() < special.end:
            return SessionPhase.REGULAR
        return SessionPhase.CLOSED

    if not cal.is_trading_day(day):
        return SessionPhase.CLOSED

    windows = windows_for(segment)
    current = when.time()

    if current < windows.pre_open_start:
        return SessionPhase.CLOSED
    if current < windows.regular_start:
        return SessionPhase.PRE_OPEN
    if current < windows.regular_end:
        return SessionPhase.REGULAR
    if current < windows.closing_end:
        return SessionPhase.CLOSING
    if current < windows.post_market_end:
        return SessionPhase.POST_MARKET
    return SessionPhase.CLOSED


def is_market_open(
    moment: Optional[datetime] = None,
    *,
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
    clock: Optional[Clock] = None,
) -> bool:
    """True only during the regular session — the only phase that accepts orders."""
    return phase_at(moment, segment=segment, calendar=calendar, clock=clock).allows_orders


def session_bounds(
    day: date,
    *,
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
) -> Optional[tuple[datetime, datetime]]:
    """Regular-session start and end for ``day``; ``None`` when closed."""
    cal = calendar or get_trading_calendar()
    special = cal.special_session(day)
    if special is not None:
        return (
            datetime.combine(day, special.start, tzinfo=IST),
            datetime.combine(day, special.end, tzinfo=IST),
        )
    if not cal.is_trading_day(day):
        return None
    windows = windows_for(segment)
    return (
        datetime.combine(day, windows.regular_start, tzinfo=IST),
        datetime.combine(day, windows.regular_end, tzinfo=IST),
    )


def minutes_until_close(
    moment: Optional[datetime] = None,
    *,
    segment: Segment = Segment.CASH,
    calendar: Optional[TradingCalendar] = None,
    clock: Optional[Clock] = None,
) -> Optional[float]:
    """Minutes to the regular close. ``None`` when the market is not open."""
    active_clock = clock or get_clock()
    when = ensure_ist(moment) if moment is not None else active_clock.now()
    if not is_market_open(when, segment=segment, calendar=calendar, clock=active_clock):
        return None
    bounds = session_bounds(when.date(), segment=segment, calendar=calendar)
    if bounds is None:  # pragma: no cover - guarded by is_market_open
        return None
    return (bounds[1] - when).total_seconds() / 60
