"""Typed exception hierarchy — ERR-001, ERR-007.

Every exception raised by application code derives from :class:`ATSError` and
carries a stable machine-readable ``code`` plus human-readable detail. The five
top-level categories drive retry and safety behaviour:

======================  ===========================================  ==========
Category                Meaning                                      Retryable
======================  ===========================================  ==========
``TransientError``      Temporary; the same call may succeed later    yes
``PermanentError``      Will not succeed on retry without a change    no
``ConfigurationError``  The system is misconfigured                   no
``DataQualityError``    Input data cannot be trusted                  no
``SafetyError``         A safety invariant would be violated          never
======================  ===========================================  ==========

``SafetyError`` is deliberately *never* retryable and must always fail closed:
it means continuing would risk money, duplicate an order, or bypass a control.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "ATSError",
    "TransientError",
    "PermanentError",
    "ConfigurationError",
    "DataQualityError",
    "SafetyError",
    "TimeoutError_",
    "ConnectionFailedError",
    "RateLimitError",
    "AuthenticationError",
    "InvalidResponseError",
    "NotFoundError",
    "ValidationError",
    "DuplicateError",
    "StaleDataError",
    "InsufficientHistoryError",
    "TradingDisabledError",
    "NotArmedError",
    "SyntheticDataInLiveError",
    "ReconciliationRequiredError",
    "RiskRejectedError",
    "ImmutableRecordError",
]


class ATSError(Exception):
    """Base class for every error raised by this application.

    Parameters
    ----------
    message:
        Human-readable detail. Never include secrets.
    code:
        Stable machine-readable code. Defaults to the class name in
        SCREAMING_SNAKE_CASE, which keeps codes stable as long as class names are.
    context:
        Structured fields for logging and API responses. Must be JSON-serialisable.
    """

    #: Subclasses may override to pin a code independent of the class name.
    default_code: Optional[str] = None
    #: Whether a caller may retry the identical operation.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code or _default_code_for(type(self))
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API responses and structured logs (ERR-007)."""
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category(),
            "retryable": self.retryable,
            "context": self.context,
        }

    @classmethod
    def category(cls) -> str:
        for base in cls.__mro__:
            if base in _CATEGORY_NAMES:
                return _CATEGORY_NAMES[base]
        return "ERROR"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


def _default_code_for(cls: type) -> str:
    name = cls.__name__
    # ``TimeoutError_`` -> ``TimeoutError``; then the common ``Error`` suffix is
    # dropped so codes read ``RISK_REJECTED`` rather than ``RISK_REJECTED_ERROR``.
    if name.endswith("_"):
        name = name[:-1]
    if name.endswith("Error") and name != "Error":
        name = name[: -len("Error")]
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append("_")
        out.append(char.upper())
    return "".join(out)


# --- Categories -----------------------------------------------------------


class TransientError(ATSError):
    """Temporary failure. Safe to retry *unless* the operation is an order."""

    retryable = True


class PermanentError(ATSError):
    """Failure that will recur until something changes. Never retried."""

    retryable = False


class ConfigurationError(ATSError):
    """The system is misconfigured. Fail fast at startup rather than trade."""

    retryable = False


class DataQualityError(ATSError):
    """Input data is missing, stale, or internally inconsistent."""

    retryable = False


class SafetyError(ATSError):
    """A safety invariant would be violated. Always fails closed, never retried."""

    retryable = False


_CATEGORY_NAMES: dict[type, str] = {
    TransientError: "TRANSIENT",
    PermanentError: "PERMANENT",
    ConfigurationError: "CONFIGURATION",
    DataQualityError: "DATA_QUALITY",
    SafetyError: "SAFETY",
}


# --- Transient ------------------------------------------------------------


class TimeoutError_(TransientError):
    """An external call exceeded its deadline.

    Named with a trailing underscore to avoid shadowing the builtin. For order
    submission a timeout must NEVER be retried blindly — see EXEC-005.
    """

    default_code = "TIMEOUT"


class ConnectionFailedError(TransientError):
    """Could not reach an external dependency."""


class RateLimitError(TransientError):
    """A rate limit was hit locally or reported by the remote side."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.context.setdefault("retry_after_seconds", retry_after_seconds)


# --- Permanent ------------------------------------------------------------


class AuthenticationError(PermanentError):
    """Credentials were rejected. Critical: disables trading (AUTH-004)."""


class InvalidResponseError(PermanentError):
    """A remote response could not be parsed into the expected shape."""


class NotFoundError(PermanentError):
    """The requested entity does not exist."""


class ValidationError(PermanentError):
    """Local validation rejected the request before it left the process."""


class DuplicateError(PermanentError):
    """The operation was already performed. Never retried (EXEC-004)."""


# --- Data quality ---------------------------------------------------------


class StaleDataError(DataQualityError):
    """Data is older than its freshness budget (MD-008, RISK-013)."""


class InsufficientHistoryError(DataQualityError):
    """Not enough historical bars to satisfy an indicator lookback (HD-007)."""


# --- Safety ---------------------------------------------------------------


class TradingDisabledError(SafetyError):
    """Trading is disabled by a health check, kill switch or emergency control."""


class NotArmedError(SafetyError):
    """LIVE mode is configured but the system has not been armed (LIVE-001)."""


class SyntheticDataInLiveError(SafetyError):
    """Synthetic or simulated data reached an execution path in LIVE (ARCH-008)."""


class ReconciliationRequiredError(SafetyError):
    """Local and broker state disagree; trading stays blocked (REC-004)."""


class RiskRejectedError(SafetyError):
    """The deterministic risk engine rejected the proposal."""

    def __init__(self, message: str, *, rule: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.rule = rule
        self.context.setdefault("rule", rule)


class ImmutableRecordError(SafetyError):
    """An append-only record was modified or deleted (DB-012, JRN-007)."""
