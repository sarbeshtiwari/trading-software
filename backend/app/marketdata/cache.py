"""Quote cache — MD-003.

One decision cycle asks for the same price several times: the strategy, the
sizer, the risk engine and the executor all need it. Without a cache that is four
API calls against a 10-per-second budget, for one instrument.

The cache is TTL-based and the TTL is short by design — it exists to deduplicate
reads *within* a cycle, not to keep prices alive across cycles. Anything older
than the TTL is a miss, and staleness is still checked separately at the point of
use (MD-008): a cache hit is not evidence of freshness.

Two backends: in-process (correct and sufficient for a single trading process)
and Redis (so a dashboard or a second worker can share the same reads).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Protocol

from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.core.logging import get_logger
from app.marketdata.models import InstrumentRef, LTPQuote

logger = get_logger("marketdata.cache")

__all__ = ["QuoteCache", "MemoryQuoteCache", "RedisQuoteCache", "create_quote_cache"]


class QuoteCache(Protocol):
    async def get(self, key: str) -> Optional[LTPQuote]: ...

    async def set(self, quote: LTPQuote, ttl_seconds: float) -> None: ...

    async def clear(self) -> None: ...


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass
class MemoryQuoteCache:
    """In-process TTL cache."""

    clock: Clock = field(default_factory=get_clock)
    stats: CacheStats = field(default_factory=CacheStats)
    _entries: dict[str, tuple[LTPQuote, float]] = field(default_factory=dict, init=False)

    async def get(self, key: str) -> Optional[LTPQuote]:
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        quote, expires_at = entry
        if self.clock.monotonic() >= expires_at:
            del self._entries[key]
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return quote

    async def set(self, quote: LTPQuote, ttl_seconds: float) -> None:
        self._entries[quote.instrument.key] = (
            quote,
            self.clock.monotonic() + ttl_seconds,
        )

    async def clear(self) -> None:
        self._entries.clear()


class RedisQuoteCache:
    """Redis-backed cache, so several processes share one set of reads."""

    def __init__(self, redis_client: Any, *, prefix: str = "ats:quote:") -> None:
        self._redis = redis_client
        self._prefix = prefix
        self.stats = CacheStats()

    async def get(self, key: str) -> Optional[LTPQuote]:
        raw = await self._redis.get(self._prefix + key)
        if raw is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return _decode(raw.decode() if isinstance(raw, bytes) else str(raw))

    async def set(self, quote: LTPQuote, ttl_seconds: float) -> None:
        await self._redis.set(
            self._prefix + quote.instrument.key,
            _encode(quote),
            px=int(ttl_seconds * 1000),
        )

    async def clear(self) -> None:
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=self._prefix + "*", count=500)
            if keys:
                await self._redis.delete(*keys)
            if cursor == 0:
                break


def _encode(quote: LTPQuote) -> str:
    return json.dumps(
        {
            "trading_symbol": quote.instrument.trading_symbol,
            "exchange": quote.instrument.exchange.value,
            "segment": quote.instrument.segment.value,
            "ltp": str(quote.ltp),
            "observed_at": quote.observed_at.isoformat(),
            "data_origin": quote.data_origin.value,
        }
    )


def _decode(raw: str) -> Optional[LTPQuote]:
    from datetime import datetime

    try:
        data = json.loads(raw)
        return LTPQuote(
            instrument=InstrumentRef(
                trading_symbol=data["trading_symbol"],
                exchange=Exchange(data["exchange"]),
                segment=Segment(data["segment"]),
            ),
            ltp=Decimal(data["ltp"]),
            observed_at=datetime.fromisoformat(data["observed_at"]),
            data_origin=DataOrigin(data["data_origin"]),
        )
    except Exception as exc:  # noqa: BLE001 - a corrupt entry is simply a miss
        logger.warning("Discarding an undecodable cache entry", extra={"error": str(exc)})
        return None


def create_quote_cache(redis_url: str) -> QuoteCache:
    """In-process cache for ``memory://``; Redis otherwise."""
    if redis_url.startswith("memory://"):
        return MemoryQuoteCache()
    try:
        from redis.asyncio import Redis  # noqa: PLC0415 - optional dependency
    except ImportError:
        logger.warning("redis is not installed; falling back to the in-process quote cache")
        return MemoryQuoteCache()
    return RedisQuoteCache(Redis.from_url(redis_url))
