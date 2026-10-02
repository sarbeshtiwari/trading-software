from datetime import date, datetime, time

import pytest

from app.core.calendar import TradingCalendar
from app.core.clock import IST
from app.core.errors import ConfigurationError


def test_election_amendment_is_not_visible_before_publication():
    before = TradingCalendar.from_file(as_of=datetime(2026, 1, 12, 12, tzinfo=IST))
    after = TradingCalendar.from_file(as_of=datetime(2026, 1, 13, tzinfo=IST))
    assert not before.is_holiday(date(2026, 1, 15))
    assert after.is_holiday(date(2026, 1, 15))


def test_budget_session_is_not_visible_before_publication():
    before = TradingCalendar.from_file(as_of=datetime(2026, 1, 16, 12, tzinfo=IST))
    after = TradingCalendar.from_file(as_of=datetime(2026, 1, 17, tzinfo=IST))
    assert before.special_session(date(2026, 2, 1)) is None
    session = after.special_session(date(2026, 2, 1))
    assert session.start == time(9, 15)
    assert session.end == time(15, 30)


def test_confirmed_cash_holidays_do_not_include_settlement_only_dates():
    calendar = TradingCalendar.from_file(as_of=datetime(2026, 10, 2, tzinfo=IST))
    for day in (date(2026, 3, 3), date(2026, 5, 28), date(2026, 9, 14)):
        assert calendar.is_holiday(day)
    assert not calendar.is_holiday(date(2026, 4, 1))
    assert calendar.special_session(date(2026, 11, 8)) is None
    assert not calendar.is_year_complete(2026)


def test_naive_calendar_cutoff_is_rejected():
    with pytest.raises(ConfigurationError, match="timezone-aware"):
        TradingCalendar.from_file(as_of=datetime(2026, 1, 1))
