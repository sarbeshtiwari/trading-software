"""Indicator cache — TA-010.

Indicators are pure functions of (series, parameters), so their results are
cacheable — but only until a new bar arrives. The cache key therefore includes
the timestamp of the last bar in the series: a new bar produces a different key
and the old entry can never be served against fresher data.

That is the whole safety property. A cache keyed only on (instrument, indicator)
would happily return yesterday's RSI today.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from app.core.clock import ensure_ist
from app.core.logging import get_logger

logger = get_logger("analysis.technical.cache")

__all__ = ["IndicatorCacheKey", "IndicatorCache", "get_indicator_cache"]


@dataclass(frozen=True)
class IndicatorCacheKey:
    instrument: str
    interval_minutes: int
    indicator: str
    params: tuple
    last_bar_ts: datetime

    @classmethod
    def build(
        cls,
        *,
        instrument: str,
        interval_minutes: int,
        indicator: str,
        params: dict[str, Any],
        last_bar_ts: datetime,
    ) -> "IndicatorCacheKey":
        return cls(
            instrument=instrument,
            interval_minutes=interval_minutes,
            indicator=indicator,
            params=tuple(sorted((key, str(value)) for key, value in params.items())),
            last_bar_ts=ensure_ist(last_bar_ts),
        )


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class IndicatorCache:
    """Bounded LRU cache of computed indicator series."""

    def __init__(self, max_entries: int = 2048) -> None:
        self._entries: OrderedDict[IndicatorCacheKey, Any] = OrderedDict()
        self._max_entries = max_entries
        self.stats = CacheStats()

    def get(self, key: IndicatorCacheKey) -> Optional[Any]:
        if key in self._entries:
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return self._entries[key]
        self.stats.misses += 1
        return None

    def set(self, key: IndicatorCacheKey, value: Any) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    def get_or_compute(self, key: IndicatorCacheKey, compute: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = compute()
        self.set(key, value)
        return value

    def invalidate_instrument(self, instrument: str) -> int:
        """Drop every entry for one instrument (e.g. after a data correction)."""
        doomed = [key for key in self._entries if key.instrument == instrument]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_cache: Optional[IndicatorCache] = None


def get_indicator_cache() -> IndicatorCache:
    global _cache
    if _cache is None:
        _cache = IndicatorCache()
    return _cache
