"""Trading calendar and session phases.

Covers EXCH-001, EXCH-002, EXCH-003.

Every boundary minute is asserted explicitly: the square-off cutoff, the entry
blackout and the expiry cutoff are all expressed against these phases, so an
off-by-one here becomes an order placed into a closed session.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.core.calendar import Holiday, SpecialSession, TradingCalendar, get_trading_calendar
from app.core.clock import IST, FakeClock
from app.core.enums import Segment, SessionPhase
from app.core.errors import ConfigurationError
from app.core.sessions import (
    is_market_open,
    minutes_until_close,
    parse_time,
    phase_at,
    session_bounds,
)

pytestmark = pytest.mark.unit

# 2026-01-05 is a Monday; 2026-01-10 a Saturday; 2026-01-26 Republic Day.
MONDAY = date(2026, 1, 5)
SATURDAY = date(2026, 1, 10)
SUNDAY = date(2026, 1, 11)
REPUBLIC_DAY = date(2026, 1, 26)


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar(
        holidays=[Holiday(day=REPUBLIC_DAY, name="Republic Day")],
        complete_years=[2026],
        source="test",
    )


# --- EXCH-001 -------------------------------------------------------------


def test_trading_calendar(calendar: TradingCalendar) -> None:
    assert calendar.is_trading_day(MONDAY)
    assert not calendar.is_trading_day(SATURDAY)
    assert not calendar.is_trading_day(SUNDAY)
    assert not calendar.is_trading_day(REPUBLIC_DAY)
    assert calendar.holiday_name(REPUBLIC_DAY) == "Republic Day"
    assert calendar.holiday_name(MONDAY) is None


def test_next_and_previous_trading_day(calendar: TradingCalendar) -> None:
    friday = date(2026, 1, 9)
    assert calendar.next_trading_day(friday) == date(2026, 1, 12)  # skips the weekend
    assert calendar.previous_trading_day(date(2026, 1, 12)) == friday
    assert calendar.next_trading_day(MONDAY, inclusive=True) == MONDAY

    # Republic Day 2026 is a Monday, so the next session is the Tuesday.
    assert calendar.next_trading_day(REPUBLIC_DAY) == date(2026, 1, 27)


def test_trading_days_between_excludes_weekends_and_holidays(
    calendar: TradingCalendar,
) -> None:
    days = calendar.trading_days_between(date(2026, 1, 23), date(2026, 1, 27))
    # Fri 23, (Sat 24, Sun 25 excluded), Mon 26 is a holiday, Tue 27.
    assert days == [date(2026, 1, 23), date(2026, 1, 27)]


def test_sessions_between_counts_trading_days(calendar: TradingCalendar) -> None:
    start = datetime(2026, 1, 5, 15, 0, tzinfo=IST)
    end = datetime(2026, 1, 9, 9, 30, tzinfo=IST)
    assert calendar.sessions_between(start, end) == 5


def test_shipped_holiday_file_loads_and_reports_incompleteness() -> None:
    """The shipped file is deliberately partial and must say so."""
    calendar = get_trading_calendar(reload=True)
    assert calendar.holiday_count > 0
    assert calendar.is_trading_day(date(2026, 1, 26)) is False

    warning = calendar.completeness_warning(2026)
    assert warning is not None
    assert "incomplete" in warning
    assert "holidays.json" in warning


def test_missing_holiday_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        TradingCalendar.from_file(tmp_path / "nope.json")


def test_complete_year_produces_no_warning(calendar: TradingCalendar) -> None:
    assert calendar.completeness_warning(2026) is None
    assert calendar.completeness_warning(2027) is not None


def test_calendar_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "holidays.json"
    path.write_text(
        json.dumps(
            {
                "source": "unit-test",
                "years": {
                    "2026": {
                        "complete": True,
                        "holidays": [{"date": "2026-03-04", "name": "Test Holiday"}],
                        "special_sessions": [
                            {
                                "date": "2026-11-08",
                                "name": "Muhurat Trading",
                                "start": "18:00",
                                "end": "19:00",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calendar = TradingCalendar.from_file(path)
    assert calendar.is_holiday(date(2026, 3, 4))
    assert calendar.is_year_complete(2026)
    special = calendar.special_session(date(2026, 11, 8))
    assert special is not None
    assert special.start == time(18, 0)


# --- EXCH-002 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (time(8, 59), SessionPhase.CLOSED),
        (time(9, 0), SessionPhase.PRE_OPEN),
        (time(9, 14), SessionPhase.PRE_OPEN),
        (time(9, 15), SessionPhase.REGULAR),
        (time(12, 0), SessionPhase.REGULAR),
        (time(15, 29), SessionPhase.REGULAR),
        (time(15, 30), SessionPhase.CLOSING),
        (time(15, 39), SessionPhase.CLOSING),
        (time(15, 40), SessionPhase.POST_MARKET),
        (time(15, 59), SessionPhase.POST_MARKET),
        (time(16, 0), SessionPhase.CLOSED),
        (time(23, 59), SessionPhase.CLOSED),
    ],
)
def test_session_phases(
    calendar: TradingCalendar, moment: time, expected: SessionPhase
) -> None:
    when = datetime.combine(MONDAY, moment, tzinfo=IST)
    assert phase_at(when, calendar=calendar) is expected


def test_only_the_regular_phase_accepts_orders(calendar: TradingCalendar) -> None:
    assert SessionPhase.REGULAR.allows_orders
    assert not SessionPhase.PRE_OPEN.allows_orders
    assert not SessionPhase.CLOSING.allows_orders
    assert not SessionPhase.POST_MARKET.allows_orders
    assert not SessionPhase.CLOSED.allows_orders

    assert is_market_open(datetime.combine(MONDAY, time(10, 0), tzinfo=IST), calendar=calendar)
    assert not is_market_open(
        datetime.combine(MONDAY, time(9, 10), tzinfo=IST), calendar=calendar
    )


def test_weekends_and_holidays_are_closed_all_day(calendar: TradingCalendar) -> None:
    for day in (SATURDAY, SUNDAY, REPUBLIC_DAY):
        for moment in (time(9, 30), time(12, 0), time(15, 0)):
            assert (
                phase_at(datetime.combine(day, moment, tzinfo=IST), calendar=calendar)
                is SessionPhase.CLOSED
            )


def test_phase_uses_the_injected_clock(calendar: TradingCalendar) -> None:
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 10, tzinfo=IST))
    assert phase_at(calendar=calendar, clock=clock) is SessionPhase.PRE_OPEN
    clock.advance(timedelta(minutes=10))
    assert phase_at(calendar=calendar, clock=clock) is SessionPhase.REGULAR


def test_fno_shares_the_equity_session(calendar: TradingCalendar) -> None:
    when = datetime.combine(MONDAY, time(10, 0), tzinfo=IST)
    assert phase_at(when, segment=Segment.FNO, calendar=calendar) is SessionPhase.REGULAR


def test_session_bounds_and_time_to_close(calendar: TradingCalendar) -> None:
    bounds = session_bounds(MONDAY, calendar=calendar)
    assert bounds is not None
    assert bounds[0].time() == time(9, 15)
    assert bounds[1].time() == time(15, 30)
    assert session_bounds(SATURDAY, calendar=calendar) is None

    when = datetime.combine(MONDAY, time(15, 0), tzinfo=IST)
    assert minutes_until_close(when, calendar=calendar) == pytest.approx(30.0)
    assert minutes_until_close(
        datetime.combine(MONDAY, time(16, 30), tzinfo=IST), calendar=calendar
    ) is None


# --- EXCH-003 -------------------------------------------------------------


def test_special_sessions() -> None:
    """Muhurat trading opens a day that would otherwise be closed."""
    muhurat_day = date(2026, 11, 8)  # a Sunday
    calendar = TradingCalendar(
        special_sessions=[
            SpecialSession(
                day=muhurat_day,
                name="Muhurat Trading",
                start=time(18, 0),
                end=time(19, 0),
            )
        ],
        complete_years=[2026],
    )

    assert calendar.is_trading_day(muhurat_day)
    assert (
        phase_at(datetime.combine(muhurat_day, time(18, 30), tzinfo=IST), calendar=calendar)
        is SessionPhase.REGULAR
    )
    # Outside the special window the day is still closed.
    assert (
        phase_at(datetime.combine(muhurat_day, time(10, 0), tzinfo=IST), calendar=calendar)
        is SessionPhase.CLOSED
    )

    bounds = session_bounds(muhurat_day, calendar=calendar)
    assert bounds is not None
    assert bounds[0].time() == time(18, 0)


def test_parse_time_accepts_configuration_values() -> None:
    assert parse_time("15:10") == time(15, 10)
    assert parse_time("09:15:00") == time(9, 15)
