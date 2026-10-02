"""Unavailable exchange hours never fall back to a regular trading session."""

from datetime import datetime, time

import pytest

from app.config import Settings
from app.core import calendar as calendar_module
from app.core.calendar import SpecialSession, TradingCalendar
from app.core.clock import IST, FakeClock
from app.core.entry_windows import entry_window
from app.core.enums import HealthStatus, SessionPhase
from app.core.errors import ConfigurationError
from app.core.sessions import phase_at, session_bounds
from app.monitoring.broker_checks import MarketStatusCheck


async def test_unknown_hours_block_weekday_and_report_health(monkeypatch):
    now = datetime(2026, 9, 21, 10, tzinfo=IST)
    calendar = TradingCalendar(
        complete_years=[2026],
        special_sessions=[SpecialSession(now.date(), "Isolated announced session", None, None)],
    )
    monkeypatch.setattr(calendar_module, "_calendar", calendar)
    assert not calendar.is_trading_day(now.date())
    assert session_bounds(now.date(), calendar=calendar) is None
    assert phase_at(now, calendar=calendar) == SessionPhase.CLOSED
    assert entry_window(now, Settings(), calendar=calendar).phase == "SPECIAL_SESSION_UNAVAILABLE"
    status, message, context = await MarketStatusCheck(FakeClock(now)).run()
    assert status == HealthStatus.DEGRADED
    assert "SPECIAL SESSION HOURS UNAVAILABLE" in message
    assert context["trading_day"] is False
    assert calendar.session_warning(now.replace(day=22).date()) is None
    assert calendar.is_trading_day(now.replace(day=22).date())


@pytest.mark.parametrize(
    "start,end", [(time(18), None), (None, time(19)), (time(19), time(18)), (time(18), time(18))]
)
def test_invalid_special_times_are_refused(start, end):
    with pytest.raises(ConfigurationError, match="paired local"):
        SpecialSession(datetime(2026, 11, 8).date(), "Isolated malformed session", start, end)
