"""Time to expiry — GRK-007.

Two conventions exist and they disagree, most visibly in the last week of an
option's life where theta is largest. Both are provided, and the choice is
explicit at every call site rather than hidden in a helper.

* **Calendar convention** (default): actual elapsed time divided by 365 days.
  This is what Black-Scholes assumes, and it is what discounting needs, because
  interest accrues on weekends.
* **Trading convention**: remaining *sessions* divided by the annual session
  count, from the trading calendar. Traders often prefer it because volatility
  does not accrue when the market is closed — a Friday-to-Monday gap is one
  trading day of volatility, not three calendar days.

Indian options expire at **15:30 IST on expiry day**. Using end-of-day would give
an option six and a half hours of value it does not have on the morning of
expiry, which is exactly when a wrong theta costs the most.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from app.core.calendar import TradingCalendar, get_trading_calendar
from app.core.clock import IST, Clock, ensure_ist, get_clock
from app.core.errors import ValidationError

__all__ = [
    "EXPIRY_TIME",
    "CALENDAR_DAYS_PER_YEAR",
    "TRADING_DAYS_PER_YEAR",
    "expiry_moment",
    "calendar_years_to_expiry",
    "trading_years_to_expiry",
    "days_to_expiry",
    "is_expiry_day",
]

#: Indian equity derivatives stop trading at 15:30 IST on expiry day.
EXPIRY_TIME = time(15, 30)

CALENDAR_DAYS_PER_YEAR = 365.0
#: Approximate NSE sessions per year, after weekends and holidays.
TRADING_DAYS_PER_YEAR = 250.0


def expiry_moment(expiry: date) -> datetime:
    """The exact instant an option expires."""
    return datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)


def calendar_years_to_expiry(
    expiry: date, *, now: Optional[datetime] = None, clock: Optional[Clock] = None
) -> float:
    """Years to expiry on the calendar convention. Zero once expired."""
    moment = ensure_ist(now) if now is not None else (clock or get_clock()).now()
    remaining = (expiry_moment(expiry) - moment).total_seconds()
    if remaining <= 0:
        return 0.0
    return remaining / (CALENDAR_DAYS_PER_YEAR * 86400.0)


def trading_years_to_expiry(
    expiry: date,
    *,
    now: Optional[datetime] = None,
    clock: Optional[Clock] = None,
    calendar: Optional[TradingCalendar] = None,
    days_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float:
    """Years to expiry counted in trading sessions.

    The current session counts fractionally: half a session remaining at lunchtime
    is half a session, not a whole one.
    """
    moment = ensure_ist(now) if now is not None else (clock or get_clock()).now()
    if moment >= expiry_moment(expiry):
        return 0.0

    cal = calendar or get_trading_calendar()
    today = moment.date()

    # Whole sessions strictly after today, up to and including expiry day.
    sessions = len(cal.trading_days_between(today + timedelta(days=1), expiry))

    # Fraction of today remaining, if today is a session.
    if cal.is_trading_day(today):
        from app.core.sessions import session_bounds

        bounds = session_bounds(today)
        if bounds is not None:
            start, end = bounds
            if moment < start:
                sessions += 1
            elif moment < end:
                total = (end - start).total_seconds()
                sessions += (end - moment).total_seconds() / total if total else 0.0

    if sessions <= 0:
        return 0.0
    return sessions / days_per_year


def days_to_expiry(
    expiry: date, *, now: Optional[datetime] = None, clock: Optional[Clock] = None
) -> int:
    """Whole calendar days remaining; 0 on expiry day, negative once past."""
    moment = ensure_ist(now) if now is not None else (clock or get_clock()).now()
    return (expiry - moment.date()).days


def sessions_to_expiry(
    expiry: date,
    *,
    now: Optional[datetime] = None,
    clock: Optional[Clock] = None,
    calendar: Optional[TradingCalendar] = None,
) -> int:
    """Whole trading sessions remaining, including expiry day itself."""
    moment = ensure_ist(now) if now is not None else (clock or get_clock()).now()
    cal = calendar or get_trading_calendar()
    if expiry < moment.date():
        return 0
    return len(cal.trading_days_between(moment.date(), expiry))


def is_expiry_day(
    expiry: date, *, now: Optional[datetime] = None, clock: Optional[Clock] = None
) -> bool:
    moment = ensure_ist(now) if now is not None else (clock or get_clock()).now()
    return moment.date() == expiry


def validate_expiry(expiry: date, *, now: Optional[datetime] = None) -> None:
    """Raise if an expiry is in the past — a contract that cannot be traded."""
    reference = ensure_ist(now).date() if now else get_clock().today()
    if expiry < reference:
        raise ValidationError(
            f"expiry {expiry.isoformat()} is in the past (today is {reference.isoformat()})",
            context={"expiry": expiry.isoformat()},
        )
