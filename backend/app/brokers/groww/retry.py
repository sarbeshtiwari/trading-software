"""Retry policy — GRW-005, and the guard behind GRW-006.

Two rules shape everything here:

1. **Only transient failures are retried.** Retrying a rejected order or a bad
   request cannot succeed and spends the rate-limit budget that the *next* real
   call needs.
2. **Order-creating calls are never retried by this layer.** A timeout on
   ``/order/create`` does not mean the order was not placed. The executor must
   first ask the broker whether it already has the order (EXEC-005); only it can
   decide what happens next. Callers express this by passing ``allow_retry=False``,
   and the retry helper refuses to retry even a transient failure in that case.

Backoff is exponential with full jitter, which spreads retries from several
callers instead of synchronising them into a second burst.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence, TypeVar

from app.core.clock import Clock, get_clock
from app.core.errors import ATSError, RateLimitError, TimeoutError_, TransientError
from app.core.logging import get_logger

logger = get_logger("brokers.groww.retry")

T = TypeVar("T")

__all__ = ["RetryPolicy", "with_retry", "RetryExhaustedError"]


class RetryExhaustedError(TransientError):
    """Every attempt failed. Carries the final underlying error."""

    def __init__(self, message: str, *, attempts: int, last_error: BaseException) -> None:
        super().__init__(message, context={"attempts": attempts, "last_error": str(last_error)})
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    #: Wall budget across all attempts, including waits.
    total_deadline_seconds: float = 30.0
    #: Exceptions considered retryable, beyond the transient category.
    retry_on: Sequence[type[BaseException]] = (TransientError,)

    def delay_for(self, attempt: int, *, rng: Optional[random.Random] = None) -> float:
        """Full-jitter exponential backoff for a 1-based attempt number."""
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
        generator = rng or random
        return generator.uniform(0.0, ceiling)


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: Optional[RetryPolicy] = None,
    description: str = "groww call",
    allow_retry: bool = True,
    clock: Optional[Clock] = None,
    rng: Optional[random.Random] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Run ``operation``, retrying only transient failures.

    ``allow_retry=False`` executes exactly once. That is the setting every
    order-creating call uses.
    """
    resolved_policy = policy or RetryPolicy()
    active_clock = clock or get_clock()
    started = active_clock.monotonic()
    attempt = 0
    last_error: Optional[BaseException] = None

    max_attempts = resolved_policy.max_attempts if allow_retry else 1

    while attempt < max_attempts:
        attempt += 1
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - classified immediately below
            last_error = exc

            retryable = isinstance(exc, tuple(resolved_policy.retry_on)) or (
                isinstance(exc, ATSError) and exc.retryable
            )
            if not allow_retry or not retryable or attempt >= max_attempts:
                if not allow_retry and retryable:
                    logger.warning(
                        "Transient failure on a non-retryable call; leaving the decision "
                        "to the caller",
                        extra={"operation": description, "error": str(exc)},
                    )
                raise

            delay = resolved_policy.delay_for(attempt, rng=rng)
            if isinstance(exc, RateLimitError) and exc.retry_after_seconds:
                delay = max(delay, exc.retry_after_seconds)

            elapsed = active_clock.monotonic() - started
            if elapsed + delay > resolved_policy.total_deadline_seconds:
                raise RetryExhaustedError(
                    f"{description} exhausted its {resolved_policy.total_deadline_seconds}s "
                    f"retry budget after {attempt} attempt(s)",
                    attempts=attempt,
                    last_error=exc,
                ) from exc

            logger.warning(
                "Retrying after a transient failure",
                extra={
                    "operation": description,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": round(delay, 3),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)

    assert last_error is not None  # noqa: S101 - the loop always sets it before exiting
    raise RetryExhaustedError(
        f"{description} failed after {attempt} attempt(s)",
        attempts=attempt,
        last_error=last_error,
    ) from last_error


def is_timeout(exc: BaseException) -> bool:
    """True for our timeout type and the underlying transport timeouts."""
    if isinstance(exc, TimeoutError_):
        return True
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return type(exc).__name__ in {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}
