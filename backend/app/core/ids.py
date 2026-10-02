"""Identifier generation — ARCH-013.

ULIDs (Universally Unique Lexicographically Sortable Identifiers) are used
throughout: 48 bits of millisecond timestamp followed by 80 bits of randomness,
encoded as 26 Crockford base-32 characters.

Why ULID rather than UUID4:

* **Sortable.** Ordering rows by id orders them by creation time, which matters
  for audit chains, order lifecycles and journal lineage.
* **Compact and URL-safe.** No hyphens, no case ambiguity (Crockford base-32
  excludes I, L, O and U).
* **Monotonic within a millisecond.** Two ids generated in the same millisecond
  still sort in creation order, so a burst of orders keeps a stable sequence.

Implemented locally rather than pulling a dependency: the algorithm is small,
and an id generator is exactly the kind of thing that should not be able to
change under the project silently.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from app.core.clock import Clock, get_clock

__all__ = [
    "new_ulid",
    "reset_ulid_state_for_testing",
    "ulid_timestamp_ms",
    "new_id",
    "proposal_id",
    "decision_id",
    "order_intent_id",
    "audit_id",
    "journal_id",
    "correlation_id",
    "to_broker_reference_id",
]

# Crockford base-32: digits plus uppercase letters excluding I, L, O, U.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {char: index for index, char in enumerate(_ALPHABET)}

_TIME_CHARS = 10
_RANDOM_CHARS = 16
_RANDOM_BITS = 80
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1
_MAX_TIME_MS = (1 << 48) - 1

_lock = threading.Lock()
_last_ms: int = -1
_last_random: int = 0


def _encode(value: int, length: int) -> str:
    chars = [""] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid(clock: Optional[Clock] = None) -> str:
    """Return a new 26-character ULID, monotonic within a millisecond."""
    global _last_ms, _last_random

    active_clock = clock or get_clock()
    now_ms = int(active_clock.utcnow().timestamp() * 1000)
    if now_ms > _MAX_TIME_MS:  # pragma: no cover - year 10889
        raise ValueError("timestamp out of ULID range")

    with _lock:
        if now_ms == _last_ms:
            # Same millisecond: increment instead of re-randomising so ordering
            # within the millisecond still reflects creation order.
            if _last_random >= _MAX_RANDOM:
                now_ms += 1
                randomness = int.from_bytes(os.urandom(10), "big")
            else:
                randomness = _last_random + 1
        elif now_ms < _last_ms:
            # Wall clock moved backwards (NTP correction). Keep ids monotonic by
            # staying on the previous millisecond rather than emitting an id that
            # sorts before already-issued ones.
            now_ms = _last_ms
            randomness = (_last_random + 1) & _MAX_RANDOM
        else:
            randomness = int.from_bytes(os.urandom(10), "big")

        _last_ms = now_ms
        _last_random = randomness

    return _encode(now_ms, _TIME_CHARS) + _encode(randomness, _RANDOM_CHARS)


def reset_ulid_state_for_testing() -> None:
    """Clear the monotonic guard.

    Test-only. The guard deliberately refuses to emit an id that sorts before one
    already issued, so a test that rewinds the clock needs to clear it first.
    Production code must never call this: doing so would allow duplicate or
    out-of-order ids across a clock correction.
    """
    global _last_ms, _last_random
    with _lock:
        _last_ms = -1
        _last_random = 0


def ulid_timestamp_ms(ulid: str) -> int:
    """Extract the millisecond timestamp encoded in a ULID."""
    if len(ulid) != _TIME_CHARS + _RANDOM_CHARS:
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for char in ulid[:_TIME_CHARS]:
        try:
            value = (value << 5) | _DECODE[char.upper()]
        except KeyError as exc:
            raise ValueError(f"invalid ULID character {char!r}") from exc
    return value


def new_id(prefix: str) -> str:
    """Return a prefixed identifier such as ``prp_01J8...``.

    The prefix makes ids self-describing in logs and audit records, which is
    worth far more than the four saved characters when reading an incident trail.
    """
    if not prefix or not prefix.isalnum():
        raise ValueError(f"prefix must be alphanumeric, got {prefix!r}")
    return f"{prefix}_{new_ulid()}"


def proposal_id() -> str:
    return new_id("prp")


def decision_id() -> str:
    return new_id("dec")


def order_intent_id() -> str:
    return new_id("oin")


def audit_id() -> str:
    return new_id("aud")


def journal_id() -> str:
    return new_id("jrn")


def correlation_id() -> str:
    return new_id("cor")


def to_broker_reference_id(intent_id: str) -> str:
    """Derive a Groww-compatible ``order_reference_id`` from an intent id.

    Groww constrains the field to 8-20 alphanumeric characters with at most two
    hyphens. A ULID is 26 characters, so the trailing 20 are used: they carry the
    low timestamp bits plus all 80 randomness bits, which keeps collisions
    negligible while staying deterministic — the same intent always derives the
    same reference id, which is what makes submission idempotent (EXEC-004).
    """
    raw = intent_id.split("_", 1)[-1]
    if len(raw) < 8:
        raise ValueError(f"cannot derive a broker reference id from {intent_id!r}")
    reference = raw[-20:].upper()
    if not reference.isalnum():
        raise ValueError(f"derived reference id is not alphanumeric: {reference!r}")
    return reference
