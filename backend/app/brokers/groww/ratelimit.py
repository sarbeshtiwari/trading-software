"""Client-side rate limiting — GRW-004.

Groww publishes per-second and per-minute limits per request category:

===============  ==========  ==========
Category         Per second  Per minute
===============  ==========  ==========
Authentication   5           30
Orders           10          250
Live data        10          300
Non-trading      20          500
===============  ==========  ==========

Both windows are enforced with **sliding-window counters**, not token buckets.
A token bucket permits a full-capacity burst on top of continuous refill, so a
250-per-minute bucket can emit up to 500 calls inside some 60-second window —
which is precisely the limit it was supposed to protect. A sliding window counts
what actually happened in the trailing window and never exceeds the published
number.

Being throttled by the broker while trying to exit a position is the expensive
failure here, so traffic is shaped locally rather than discovered remotely.

The limiter is *shared across the process*: a per-client limiter would let two
clients each spend the full budget.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.core.clock import Clock, get_clock
from app.core.logging import get_logger

logger = get_logger("brokers.groww.ratelimit")

__all__ = [
    "RateCategory",
    "SlidingWindowCounter",
    "RateLimiter",
    "DOCUMENTED_LIMITS",
    "get_rate_limiter",
    "reset_rate_limiter",
]


class RateCategory(str, Enum):
    AUTH = "auth"
    ORDERS = "orders"
    LIVE_DATA = "live_data"
    NON_TRADING = "non_trading"


#: category -> (per second, per minute), from the official documentation.
DOCUMENTED_LIMITS: dict[RateCategory, tuple[int, int]] = {
    RateCategory.AUTH: (5, 30),
    RateCategory.ORDERS: (10, 250),
    RateCategory.LIVE_DATA: (10, 300),
    RateCategory.NON_TRADING: (20, 500),
}


@dataclass
class SlidingWindowCounter:
    """At most ``limit`` grants in any trailing ``window_seconds``.

    Timestamps come from the monotonic clock: a wall-clock correction must not
    hand out a windfall of permits.
    """

    limit: int
    window_seconds: float
    clock: Clock = field(default_factory=get_clock)
    _grants: deque = field(default_factory=deque, init=False)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._grants and self._grants[0] <= cutoff:
            self._grants.popleft()

    def used(self) -> int:
        self._prune(self.clock.monotonic())
        return len(self._grants)

    def available(self) -> int:
        return max(0, self.limit - self.used())

    def try_take(self) -> bool:
        now = self.clock.monotonic()
        self._prune(now)
        if len(self._grants) >= self.limit:
            return False
        self._grants.append(now)
        return True

    def seconds_until_free(self) -> float:
        """How long until one more grant would be permitted."""
        now = self.clock.monotonic()
        self._prune(now)
        if len(self._grants) < self.limit:
            return 0.0
        # The oldest grant leaving the window frees exactly one slot.
        return max(0.0, self._grants[0] + self.window_seconds - now)


class RateLimiter:
    """Enforces both windows for every category."""

    def __init__(
        self,
        limits: Optional[dict[RateCategory, tuple[int, int]]] = None,
        *,
        clock: Optional[Clock] = None,
    ) -> None:
        self._clock = clock or get_clock()
        self._limits = dict(limits or DOCUMENTED_LIMITS)
        self._windows: dict[RateCategory, tuple[SlidingWindowCounter, SlidingWindowCounter]] = {
            category: (
                SlidingWindowCounter(per_second, 1.0, clock=self._clock),
                SlidingWindowCounter(per_minute, 60.0, clock=self._clock),
            )
            for category, (per_second, per_minute) in self._limits.items()
        }
        self._locks: dict[RateCategory, asyncio.Lock] = {
            category: asyncio.Lock() for category in self._windows
        }
        #: Diagnostics, surfaced by the metrics endpoint.
        self.waits: dict[RateCategory, int] = {category: 0 for category in self._windows}

    def limits_for(self, category: RateCategory) -> tuple[int, int]:
        return self._limits[category]

    def usage(self, category: RateCategory) -> tuple[int, int]:
        per_second, per_minute = self._windows[category]
        return per_second.used(), per_minute.used()

    def try_acquire(self, category: RateCategory) -> bool:
        """Non-blocking attempt. Both windows must allow it."""
        per_second, per_minute = self._windows[category]
        # Check both before spending either: a partial spend leaks a permit.
        if per_second.available() < 1 or per_minute.available() < 1:
            return False
        return per_second.try_take() and per_minute.try_take()

    def delay_for(self, category: RateCategory) -> float:
        per_second, per_minute = self._windows[category]
        return max(per_second.seconds_until_free(), per_minute.seconds_until_free())

    async def acquire(self, category: RateCategory, *, timeout: Optional[float] = None) -> None:
        """Wait until a call in this category is permitted.

        Serialised per category so two callers cannot both observe the same free
        permit and then both spend it.
        """
        deadline = None if timeout is None else self._clock.monotonic() + timeout
        async with self._locks[category]:
            while True:
                if self.try_acquire(category):
                    return
                delay = max(self.delay_for(category), 0.001)
                if deadline is not None and self._clock.monotonic() + delay > deadline:
                    from app.core.errors import RateLimitError

                    raise RateLimitError(
                        f"Local rate limit for {category.value} would exceed the "
                        f"{timeout}s budget",
                        retry_after_seconds=delay,
                        context={"category": category.value},
                    )
                self.waits[category] += 1
                logger.debug(
                    "Rate limiter pausing",
                    extra={"category": category.value, "delay_seconds": round(delay, 3)},
                )
                await asyncio.sleep(delay)


_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """The process-wide limiter. One budget, shared by every client."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_rate_limiter(limiter: Optional[RateLimiter] = None) -> RateLimiter:
    """Replace the process limiter (tests, and re-configuration at startup)."""
    global _limiter
    _limiter = limiter or RateLimiter()
    return _limiter
