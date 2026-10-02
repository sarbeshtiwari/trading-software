"""Time authority — ARCH-012.

Every timestamp in the system comes from a :class:`Clock`. Production code must
never call :func:`datetime.datetime.now` directly: the layering test asserts it.

Two reasons this matters for a trading system:

1. **Timezone.** Indian markets run in ``Asia/Kolkata``. A naive datetime that
   silently picks up the host timezone will mis-classify session boundaries.
2. **Testability.** Session gating, staleness, expiry, square-off cutoffs and
   arming expiry are all time-dependent safety behaviours. They are only
   testable if time can be injected and controlled.

Monotonic time is exposed separately from wall-clock time: latency and timeout
measurement must not be affected by a wall-clock adjustment.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

__all__ = [
    "IST",
    "UTC",
    "Clock",
    "SystemClock",
    "FakeClock",
    "get_clock",
    "set_clock",
    "reset_clock",
    "ensure_ist",
    "to_ist",
    "to_utc",
]

#: India Standard Time — the only wall-clock timezone this system reasons in.
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


@runtime_checkable
class Clock(Protocol):
    """Source of time. Implementations must return timezone-aware datetimes."""

    def now(self) -> datetime:
        """Current wall-clock time in IST (timezone-aware)."""

    def utcnow(self) -> datetime:
        """Current wall-clock time in UTC (timezone-aware)."""

    def today(self) -> date:
        """Current calendar date in IST."""

    def monotonic(self) -> float:
        """Monotonically increasing seconds, unaffected by clock adjustments."""


class SystemClock:
    """Real time, from the operating system."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(tz=IST)

    def utcnow(self) -> datetime:
        return datetime.now(tz=UTC)

    def today(self) -> date:
        return self.now().date()

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Controllable clock for tests.

    ``FakeClock`` is production-safe to import but must only be *installed* by
    tests. Wall-clock and monotonic time advance together unless advanced
    separately, which lets a test simulate a wall-clock jump independent of
    elapsed real time.
    """

    __slots__ = ("_now", "_monotonic")

    def __init__(self, start: Optional[datetime] = None, monotonic_start: float = 0.0) -> None:
        if start is None:
            start = datetime(2026, 1, 1, 9, 15, tzinfo=IST)
        self._now = ensure_ist(start)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        return self._now

    def utcnow(self) -> datetime:
        return self._now.astimezone(UTC)

    def today(self) -> date:
        return self._now.date()

    def monotonic(self) -> float:
        return self._monotonic

    # --- test controls ---------------------------------------------------

    def advance(self, delta: timedelta) -> datetime:
        """Advance both wall-clock and monotonic time by ``delta``."""
        self._now = self._now + delta
        self._monotonic += delta.total_seconds()
        return self._now

    def advance_seconds(self, seconds: float) -> datetime:
        return self.advance(timedelta(seconds=seconds))

    def set_to(self, moment: datetime) -> datetime:
        """Jump wall-clock time without advancing monotonic time."""
        self._now = ensure_ist(moment)
        return self._now


# --- Injection ------------------------------------------------------------

_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """Return the installed clock. All application code uses this."""
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install a clock (tests and startup wiring only). Returns the previous one."""
    global _clock
    previous = _clock
    _clock = clock
    return previous


def reset_clock() -> None:
    """Restore the real system clock."""
    global _clock
    _clock = SystemClock()


# --- Conversions ----------------------------------------------------------


def ensure_ist(moment: datetime) -> datetime:
    """Return ``moment`` as an IST-aware datetime.

    A naive datetime is *interpreted* as IST rather than as the host timezone,
    because every naive datetime in this domain (session times, expiry times,
    cutoffs) is an Indian market time.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def to_ist(moment: datetime) -> datetime:
    """Convert any aware datetime to IST. Naive input is treated as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).astimezone(IST)
    return moment.astimezone(IST)


def to_utc(moment: datetime) -> datetime:
    """Convert any datetime to UTC. Naive input is treated as IST."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST).astimezone(UTC)
    return moment.astimezone(UTC)
