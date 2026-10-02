"""Groww error taxonomy — GRW-003.

Maps the documented Groww error codes onto the application exception hierarchy so
the rest of the system never inspects a broker-specific string.

The distinction that matters most is **retryable vs not**:

* ``GA000`` (internal) and ``GA003`` (cannot serve right now) are transient.
* ``GA001``/``GA004``/``GA005``/``GA006`` are permanent — retrying an invalid
  request just spends the rate-limit budget.
* ``GA007`` (duplicate ``order_reference_id``) is special: it is *not* a failure.
  It means the broker already has this order, which is exactly what the
  idempotency key exists to detect. Retrying is forbidden; the caller must look
  the existing order up instead (EXEC-004, EXEC-005).
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.errors import (
    ATSError,
    AuthenticationError,
    DuplicateError,
    NotFoundError,
    PermanentError,
    TransientError,
    ValidationError,
)

__all__ = [
    "GrowwError",
    "GrowwTransientError",
    "GrowwPermanentError",
    "GrowwAuthError",
    "GrowwNotFoundError",
    "GrowwBadRequestError",
    "DuplicateOrderReferenceError",
    "map_groww_error",
    "GROWW_ERROR_CODES",
]


#: The documented code set, verified 2026-09-17.
GROWW_ERROR_CODES: dict[str, str] = {
    "GA000": "Internal error occurred",
    "GA001": "Bad request",
    "GA003": "Unable to serve request currently",
    "GA004": "Requested entity does not exist",
    "GA005": "User not authorised to perform this operation",
    "GA006": "Cannot process this request",
    "GA007": "Duplicate order reference id",
}


class GrowwError(ATSError):
    """Base for anything the Groww API reported.

    Carries the broker code separately from our own code so both appear in logs
    and in the order record.
    """

    def __init__(
        self,
        message: str,
        *,
        broker_code: Optional[str] = None,
        http_status: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        merged: dict[str, Any] = dict(context or {})
        if broker_code:
            merged.setdefault("broker_code", broker_code)
        if http_status is not None:
            merged.setdefault("http_status", http_status)
        super().__init__(message, context=merged)
        self.broker_code = broker_code
        self.http_status = http_status


class GrowwTransientError(GrowwError, TransientError):
    """Temporary broker-side failure. Retryable — except on the order path."""

    retryable = True


class GrowwPermanentError(GrowwError, PermanentError):
    """The request will not succeed as sent. Never retried."""

    retryable = False


class GrowwBadRequestError(GrowwPermanentError, ValidationError):
    """``GA001`` — the request was malformed or violated a broker rule."""


class GrowwAuthError(GrowwError, AuthenticationError):
    """``GA005`` or HTTP 401/403 — credentials rejected or scope missing."""

    retryable = False


class GrowwNotFoundError(GrowwError, NotFoundError):
    """``GA004`` — no such order, instrument or entity."""

    retryable = False


class DuplicateOrderReferenceError(GrowwError, DuplicateError):
    """``GA007`` — the broker already has an order with this reference id.

    Never retried. The executor resolves it by fetching the existing order by
    reference id, which is precisely how a timed-out submission is made safe.
    """

    retryable = False


#: Code → exception class. Anything unknown becomes a permanent error: treating
#: an unrecognised code as retryable would hammer the API on a novel failure.
_CODE_MAP: dict[str, type[GrowwError]] = {
    "GA000": GrowwTransientError,
    "GA001": GrowwBadRequestError,
    "GA003": GrowwTransientError,
    "GA004": GrowwNotFoundError,
    "GA005": GrowwAuthError,
    "GA006": GrowwPermanentError,
    "GA007": DuplicateOrderReferenceError,
}


def map_groww_error(
    code: Optional[str],
    message: Optional[str] = None,
    *,
    http_status: Optional[int] = None,
    context: Optional[dict[str, Any]] = None,
) -> GrowwError:
    """Build the right exception for a Groww failure response."""
    normalised = (code or "").strip().upper() or None

    if normalised in _CODE_MAP:
        error_cls = _CODE_MAP[normalised]
    elif http_status in (401, 403):
        error_cls = GrowwAuthError
    elif http_status == 404:
        error_cls = GrowwNotFoundError
    elif http_status == 429:
        error_cls = GrowwTransientError
    elif http_status is not None and http_status >= 500:
        error_cls = GrowwTransientError
    else:
        error_cls = GrowwPermanentError

    detail = message or GROWW_ERROR_CODES.get(normalised or "", "Unknown Groww error")
    prefix = f"[{normalised}] " if normalised else ""
    return error_cls(
        f"{prefix}{detail}",
        broker_code=normalised,
        http_status=http_status,
        context=context,
    )
