"""Distributed locking — ARCH-011.

Two processes trading the same account is a silent doubling of position size, so
the trading loop holds an exclusive, fenced lock for the account it acts on.

The lock is built on a small :class:`LockStore` interface with two
implementations:

* :class:`RedisLockStore` — the production path, using ``SET NX PX`` and
  compare-and-delete so a lock is only ever released by its own holder.
* :class:`MemoryLockStore` — an in-process store used by tests and by
  single-process PAPER runs on a machine with no Redis. It implements identical
  semantics, so the same lock logic is exercised either way.

The store is chosen from the configured URL: ``memory://`` selects the in-process
store, anything else is treated as a Redis URL.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Optional, Protocol, runtime_checkable

from app.core.clock import Clock, get_clock
from app.core.errors import ConfigurationError, SafetyError
from app.core.logging import get_logger

logger = get_logger("core.locks")

__all__ = [
    "LockStore",
    "MemoryLockStore",
    "RedisLockStore",
    "DistributedLock",
    "LockAcquisitionError",
    "create_lock_store",
    "trading_lock_key",
]


class LockAcquisitionError(SafetyError):
    """Another process already holds the lock. Fails closed — never proceed."""


@runtime_checkable
class LockStore(Protocol):
    """Minimal key/value operations needed for a correct lock."""

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        """Set ``key`` to ``token`` only if absent. True when acquired."""

    async def extend(self, key: str, token: str, ttl_ms: int) -> bool:
        """Extend the TTL only if ``key`` still holds ``token``."""

    async def release(self, key: str, token: str) -> bool:
        """Delete ``key`` only if it still holds ``token``."""

    async def owner(self, key: str) -> Optional[str]:
        """Return the current holder's token, if any."""

    async def close(self) -> None:
        """Release any underlying resources."""


class MemoryLockStore:
    """In-process lock store with real TTL semantics.

    Correct for a single process; deliberately not correct across processes,
    which is why production uses Redis. Selecting this store in a mode that can
    reach a real broker is refused by :func:`create_lock_store`.
    """

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self._clock = clock or get_clock()
        self._entries: dict[str, tuple[str, float]] = {}
        self._mutex = asyncio.Lock()

    def _purge_expired(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None and entry[1] <= self._clock.monotonic():
            self._entries.pop(key, None)

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        async with self._mutex:
            self._purge_expired(key)
            if key in self._entries:
                return False
            self._entries[key] = (token, self._clock.monotonic() + ttl_ms / 1000)
            return True

    async def extend(self, key: str, token: str, ttl_ms: int) -> bool:
        async with self._mutex:
            self._purge_expired(key)
            entry = self._entries.get(key)
            if entry is None or entry[0] != token:
                return False
            self._entries[key] = (token, self._clock.monotonic() + ttl_ms / 1000)
            return True

    async def release(self, key: str, token: str) -> bool:
        async with self._mutex:
            self._purge_expired(key)
            entry = self._entries.get(key)
            if entry is None or entry[0] != token:
                return False
            del self._entries[key]
            return True

    async def owner(self, key: str) -> Optional[str]:
        async with self._mutex:
            self._purge_expired(key)
            entry = self._entries.get(key)
            return entry[0] if entry else None

    async def close(self) -> None:
        self._entries.clear()


# Compare-and-act scripts. Executed server-side so the check and the mutation are
# atomic — without this, a lock can be released by a process whose lease expired.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLockStore:
    """Redis-backed lock store using ``SET NX PX`` plus compare-and-delete."""

    def __init__(self, redis_client: object) -> None:
        self._redis = redis_client
        self._release = None
        self._extend = None

    def _scripts(self) -> tuple[object, object]:
        if self._release is None or self._extend is None:
            self._release = self._redis.register_script(_RELEASE_SCRIPT)  # type: ignore[attr-defined]
            self._extend = self._redis.register_script(_EXTEND_SCRIPT)  # type: ignore[attr-defined]
        return self._release, self._extend

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        result = await self._redis.set(key, token, nx=True, px=ttl_ms)  # type: ignore[attr-defined]
        return bool(result)

    async def extend(self, key: str, token: str, ttl_ms: int) -> bool:
        _, extend = self._scripts()
        result = await extend(keys=[key], args=[token, ttl_ms])  # type: ignore[operator]
        return bool(result)

    async def release(self, key: str, token: str) -> bool:
        release, _ = self._scripts()
        result = await release(keys=[key], args=[token])  # type: ignore[operator]
        return bool(result)

    async def owner(self, key: str) -> Optional[str]:
        value = await self._redis.get(key)  # type: ignore[attr-defined]
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def close(self) -> None:
        close = getattr(self._redis, "aclose", None) or getattr(self._redis, "close", None)
        if close is not None:
            await close()


def create_lock_store(redis_url: str, *, allow_memory: bool = True) -> LockStore:
    """Build a lock store from a URL.

    ``memory://`` selects the in-process store. It is refused when
    ``allow_memory`` is false — which is how callers express "this deployment can
    reach a real broker, so a single-process lock is not good enough".
    """
    if redis_url.startswith("memory://"):
        if not allow_memory:
            raise ConfigurationError(
                "REDIS_URL=memory:// gives an in-process lock only, which cannot prevent "
                "two processes trading the same account. Configure a real Redis instance "
                "before using a mode that reaches a real broker.",
                context={"redis_url": redis_url},
            )
        logger.warning(
            "Using the in-process lock store (memory://). Safe for a single-process "
            "PAPER run only."
        )
        return MemoryLockStore()

    try:
        from redis.asyncio import Redis  # noqa: PLC0415 - optional dependency
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ConfigurationError(
            "redis package is not installed but REDIS_URL points at a Redis server. "
            "Install it (`pip install redis`) or set REDIS_URL=memory:// for a "
            "single-process PAPER run.",
            context={"redis_url": redis_url},
        ) from exc

    return RedisLockStore(Redis.from_url(redis_url))


def trading_lock_key(account_id: str) -> str:
    """Canonical lock key for the trading loop of one account."""
    return f"ats:lock:trading:{account_id}"


class DistributedLock:
    """An exclusive, fenced lock with automatic lease renewal.

    Usage::

        async with DistributedLock(store, trading_lock_key("primary")) as lock:
            ...  # only one holder across the deployment

    The lease is renewed by a background task at half the TTL. If renewal fails —
    meaning the lease was lost — the lock marks itself lost; callers check
    :attr:`held` before acting on anything that must be exclusive.
    """

    def __init__(
        self,
        store: LockStore,
        key: str,
        *,
        ttl_seconds: int = 60,
        owner: str = "trading-loop",
    ) -> None:
        self._store = store
        self._key = key
        self._ttl_ms = int(ttl_seconds * 1000)
        self._token = f"{owner}:{secrets.token_hex(8)}"
        self._renewal: Optional[asyncio.Task[None]] = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def token(self) -> str:
        return self._token

    @property
    def key(self) -> str:
        return self._key

    async def acquire(self) -> "DistributedLock":
        acquired = await self._store.acquire(self._key, self._token, self._ttl_ms)
        if not acquired:
            current = await self._store.owner(self._key)
            raise LockAcquisitionError(
                f"Could not acquire {self._key}: it is held by {current!r}. "
                f"Another instance is already trading this account; this process will not start "
                f"its trading loop.",
                context={"key": self._key, "held_by": current},
            )
        self._held = True
        self._renewal = asyncio.create_task(self._renew_forever())
        logger.info("Acquired exclusive lock", extra={"lock_key": self._key})
        return self

    async def _renew_forever(self) -> None:
        interval = max(self._ttl_ms / 2000, 0.5)
        try:
            while self._held:
                await asyncio.sleep(interval)
                if not self._held:
                    return
                extended = await self._store.extend(self._key, self._token, self._ttl_ms)
                if not extended:
                    self._held = False
                    logger.error(
                        "Lost exclusive lock lease; this process must stop acting on the "
                        "account immediately",
                        extra={"lock_key": self._key},
                    )
                    return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    async def release(self) -> None:
        if self._renewal is not None:
            self._renewal.cancel()
            try:
                await self._renewal
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._renewal = None
        if self._held:
            released = await self._store.release(self._key, self._token)
            self._held = False
            if released:
                logger.info("Released exclusive lock", extra={"lock_key": self._key})
            else:
                logger.warning(
                    "Lock was not released by this holder (lease already expired or taken)",
                    extra={"lock_key": self._key},
                )

    async def __aenter__(self) -> "DistributedLock":
        return await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()
