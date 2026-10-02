"""Shared calendar-based entry windows; exits never depend on entry eligibility."""

from datetime import timedelta

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.core.calendar import get_trading_calendar
from app.core.clock import IST
from app.core.sessions import parse_time, session_bounds


class EntryWindow(EvidenceModel):
    phase: str
    calendar_source: str
    session_start: AwareDatetime | None = None
    session_close: AwareDatetime | None = None
    entries_start: AwareDatetime | None = None
    entries_end: AwareDatetime | None = None
    squareoff_at: AwareDatetime | None = None


def entry_window(moment, settings, *, calendar=None):
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("entry window requires timezone-aware time")
    calendar = calendar or get_trading_calendar()
    now = moment.astimezone(IST)
    identity = {"calendar_source": calendar.source}
    if not calendar.is_year_complete(now.year):
        return EntryWindow(phase="CALENDAR_UNAVAILABLE", **identity)
    bounds = session_bounds(now.date(), calendar=calendar)
    if bounds is None:
        return EntryWindow(phase="MARKET_CLOSE", **identity)
    start, close = bounds
    configured = parse_time(settings.intraday_squareoff_time)
    cutoff = min(
        close, now.replace(hour=configured.hour, minute=configured.minute, second=0, microsecond=0)
    )
    if cutoff <= start:
        cutoff = close - timedelta(minutes=settings.entry_blackout_close_minutes)
    cutoff = max(start, cutoff)
    entries_start = min(close, start + timedelta(minutes=settings.entry_blackout_open_minutes))
    entries_end = max(start, cutoff - timedelta(minutes=settings.entry_blackout_close_minutes))
    phases = (
        (start, "PRE_MARKET"),
        (entries_start, "MARKET_OPEN"),
        (entries_end, "INTRADAY"),
        (cutoff, "NO_ENTRY_WINDOW"),
        (close, "EXIT_WINDOW"),
        (close + timedelta(minutes=10), "EOD_RECONCILIATION"),
    )
    phase = next((label for boundary, label in phases if now < boundary), "MARKET_CLOSE")
    if entries_start >= entries_end and start <= now < cutoff:
        phase = "NO_ENTRY_WINDOW"
    return EntryWindow(
        phase=phase,
        **identity,
        session_start=start,
        session_close=close,
        entries_start=entries_start,
        entries_end=entries_end,
        squareoff_at=cutoff,
    )
