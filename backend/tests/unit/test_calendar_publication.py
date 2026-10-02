import json
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


@pytest.mark.parametrize("publication", [None, "2026-01-13T00:00:00+05:30"])
def test_completeness_requires_available_timestamp(tmp_path, publication):
    path = tmp_path / "calendar.json"
    year = {
        "complete": True,
        "holidays": [{"date": "2026-01-15", "name": "Isolated test closure"}],
    }
    if publication is not None:
        year["available_at"] = publication
    path.write_text(json.dumps({"years": {"2026": year}}), encoding="utf-8")
    before = TradingCalendar.from_file(path, as_of=datetime(2026, 1, 12, 23, 59, tzinfo=IST))
    assert not before.is_year_complete(2026)
    assert before.completeness_warning(2026) is not None
    after = TradingCalendar.from_file(path, as_of=datetime(2026, 1, 13, tzinfo=IST))
    assert after.is_year_complete(2026) == (publication is not None)
    if publication is not None:
        assert not before.is_holiday(date(2026, 1, 15))
        assert after.is_holiday(date(2026, 1, 15))


def test_naive_year_availability_fails_closed(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "years": {
                    "2026": {
                        "complete": True,
                        "available_at": "2026-01-13T00:00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="timezone-aware"):
        TradingCalendar.from_file(path, as_of=datetime(2026, 2, 1, tzinfo=IST))
