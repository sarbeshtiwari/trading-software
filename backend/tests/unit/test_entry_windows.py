"""Configured entry boundaries against explicit isolated exchange calendars."""

from datetime import datetime, time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.calendar import SpecialSession, TradingCalendar
from app.core.clock import IST
from app.core.entry_windows import entry_window


def test_regular_and_special_boundaries():
    settings = Settings()
    calendar = TradingCalendar(complete_years=[2026], source="isolated")
    for value, expected in (
        ("09:14:59", "PRE_MARKET"),
        ("09:15:00", "MARKET_OPEN"),
        ("09:19:59", "MARKET_OPEN"),
        ("09:20:00", "INTRADAY"),
        ("14:49:59", "INTRADAY"),
        ("14:50:00", "NO_ENTRY_WINDOW"),
        ("15:10:00", "EXIT_WINDOW"),
        ("15:30:00", "EOD_RECONCILIATION"),
        ("15:40:00", "MARKET_CLOSE"),
    ):
        now = datetime.fromisoformat("2026-09-21T" + value).replace(tzinfo=IST)
        assert entry_window(now, settings, calendar=calendar).phase == expected
    now = datetime(2026, 9, 20, 18, 5, tzinfo=IST)
    special = TradingCalendar(
        complete_years=[2026],
        special_sessions=[SpecialSession(now.date(), "isolated", time(18), time(19))],
    )
    assert entry_window(now, settings, calendar=special).phase == "INTRADAY"
    assert (
        entry_window(now.replace(minute=20), settings, calendar=special).phase == "NO_ENTRY_WINDOW"
    )
    assert entry_window(now, settings, calendar=calendar).phase == "MARKET_CLOSE"
    assert entry_window(now, settings, calendar=TradingCalendar()).phase == "CALENDAR_UNAVAILABLE"
    settings.entry_blackout_open_minutes = 90
    assert entry_window(now, settings, calendar=special).phase != "INTRADAY"
    assert entry_window(now.replace(hour=19), settings, calendar=special).phase == "EOD_RECONCILIATION"
    with pytest.raises(ValueError, match="timezone"):
        entry_window(now.replace(tzinfo=None), settings, calendar=calendar)


def test_window_configuration_rejects_invalid_values():
    for changes in (
        {"entry_blackout_open_minutes": -1},
        {"entry_blackout_close_minutes": -1},
        {"intraday_squareoff_time": "25:00"},
        {"intraday_squareoff_time": "15:10:01"},
        {"intraday_squareoff_time": "15:10+05:30"},
    ):
        with pytest.raises(ValidationError):
            Settings(**changes)
