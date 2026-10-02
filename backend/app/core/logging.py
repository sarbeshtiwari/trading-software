"""Structured logging with secret redaction — LOG-001…LOG-005, SEC-004, BE-010.

Design points
-------------
* **JSON lines.** Every record is a single JSON object with both IST and UTC
  timestamps. IST is what a trader reads; UTC is what survives comparison with
  broker/exchange records.
* **Redaction at the handler level.** Secrets are scrubbed by a filter attached
  to every handler, so no code path — including third-party libraries — can leak
  a token into a log file. Redaction covers registered secret *values* and
  common secret-shaped patterns.
* **Correlation ids.** A context variable carries a correlation id through async
  call stacks, so one request/decision cycle can be reconstructed end to end.
* **Separate streams.** Application, trading decisions, broker I/O and errors go
  to their own rotating files, because during an incident you want the broker
  conversation without the rest of the noise.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Match, Optional, Pattern

from app.core.clock import UTC, to_ist

__all__ = [
    "configure_logging",
    "get_logger",
    "decision_logger",
    "broker_logger",
    "register_secret",
    "register_secrets",
    "clear_secrets",
    "set_correlation_id",
    "get_correlation_id",
    "correlation_scope",
    "RedactionFilter",
    "JsonFormatter",
    "REDACTED",
]

REDACTED = "***REDACTED***"

#: Marks handlers this module installed, so a reconfigure only replaces its own.
_OWNED_ATTR = "_ats_owned_handler"

ROOT_LOGGER_NAME = "ats"
DECISION_LOGGER_NAME = "ats.decisions"
BROKER_LOGGER_NAME = "ats.broker"

_correlation_id: ContextVar[Optional[str]] = ContextVar("ats_correlation_id", default=None)

# Values registered at configuration time (tokens, keys, passwords).
_secret_values: set[str] = set()


def _keep_first_group(match: Match[str]) -> str:
    """Keep the scheme/field name, drop the value after it."""
    return f"{match.group(1)} {REDACTED}"


def _redact_whole(match: Match[str]) -> str:
    return REDACTED


def _keep_field_name(match: Match[str]) -> str:
    """Keep ``api_key:`` and its quoting, replace only the value."""
    quote = match.group(2)
    return f"{match.group(1)}{quote}{REDACTED}{quote}"


# Secret-shaped patterns, redacted even when the exact value was never
# registered. Each pattern is paired with a replacement that keeps enough
# context for the line to stay readable while removing the secret itself.
_SECRET_PATTERNS: tuple[tuple[Pattern[str], Callable[[Match[str]], str]], ...] = (
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
        _keep_first_group,
    ),
    # Anthropic keys identify themselves, so the whole match goes.
    (
        re.compile(r"(?i)\bsk-ant-[A-Za-z0-9._\-]{8,}"),
        _redact_whole,
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|api[_-]?secret|secret|token|totp|password|passwd|"
            r"authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token)[\"']?"
            r"\s*[:=]\s*)([\"']?)([^\s,;}\"']{4,})\2"
        ),
        _keep_field_name,
    ),
)

# Keys whose values are always redacted when logged as structured fields.
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "apikey",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "totp",
        "totp_secret",
        "totp_token",
        "password",
        "passwd",
        "authorization",
        "auth",
        "anthropic_api_key",
        "groww_api_key",
        "groww_api_secret",
        "groww_totp_secret",
        "groww_totp_token",
        "jwt_secret",
        "telegram_bot_token",
        "smtp_password",
    }
)

_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


# --- Secret registry ------------------------------------------------------


def register_secret(value: Optional[str]) -> None:
    """Register a secret value for redaction. Very short values are ignored.

    A two-character "secret" would match ordinary words and redact half of every
    message, which makes logs useless and hides real problems.
    """
    if value and len(str(value)) >= 4:
        _secret_values.add(str(value))


def register_secrets(values: Iterable[Optional[str]]) -> None:
    for value in values:
        register_secret(value)


def clear_secrets() -> None:
    """Test helper: forget all registered secrets."""
    _secret_values.clear()


# --- Correlation ids ------------------------------------------------------


def set_correlation_id(value: Optional[str]) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


class correlation_scope:
    """Context manager binding a correlation id for the enclosed block."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._token: Any = None

    def __enter__(self) -> str:
        self._token = _correlation_id.set(self._value)
        return self._value

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _correlation_id.reset(self._token)


# --- Redaction ------------------------------------------------------------


def _scrub_text(text: str) -> str:
    for secret in _secret_values:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub(value: Any, *, key: Optional[str] = None) -> Any:
    if key is not None and key.lower() in _SECRET_FIELD_NAMES:
        return REDACTED
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(item) for item in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def redact_data(value: Any) -> Any:
    """Apply the shared secret policy before persisting structured audit data."""
    return _scrub(value)


class RedactionFilter(logging.Filter):
    """Scrub secrets from the message, args and structured extras of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub_text(record.msg)
        if record.args:
            record.args = _scrub(record.args)  # type: ignore[assignment]
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            record.__dict__[key] = _scrub(value, key=key)
        return True


class ContextFilter(logging.Filter):
    """Attach the current correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = get_correlation_id()
        return True


# --- Formatting -----------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Decimals serialise as strings: a float in the log would suggest a float
        # in the code, which is exactly what ARCH-014 forbids.
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object per line (LOG-001)."""

    def format(self, record: logging.LogRecord) -> str:
        utc_dt = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "ts_utc": utc_dt.isoformat(timespec="milliseconds"),
            "ts_ist": to_ist(utc_dt).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)

        return json.dumps(payload, ensure_ascii=False, default=_json_safe)


class PlainFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s [%(correlation_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = None
        return super().format(record)


# --- Configuration --------------------------------------------------------

_configured = False


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    log_dir: Optional[Path] = None,
    secrets: Iterable[Optional[str]] = (),
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
    force: bool = False,
) -> None:
    """Install handlers, filters and formatters (LOG-001, LOG-002).

    Safe to call more than once; subsequent calls are no-ops unless ``force``.
    """
    global _configured
    if _configured and not force:
        return

    register_secrets(secrets)

    resolved_level = getattr(logging, str(level).upper(), logging.INFO)
    formatter: logging.Formatter = (
        JsonFormatter() if str(log_format).lower() == "json" else PlainFormatter()
    )
    redaction = RedactionFilter()
    context = ContextFilter()

    root = logging.getLogger()
    # Remove only handlers this function installed. Anything else attached to the
    # root logger belongs to the host process (a test harness, a supervisor) and
    # silently detaching it would hide output the operator asked for.
    for name in (None, DECISION_LOGGER_NAME, BROKER_LOGGER_NAME):
        target = logging.getLogger(name) if name else root
        for handler in list(target.handlers):
            if getattr(handler, _OWNED_ATTR, False):
                target.removeHandler(handler)
                handler.close()
    root.setLevel(resolved_level)

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(redaction)
    stream.addFilter(context)
    setattr(stream, _OWNED_ATTR, True)
    root.addHandler(stream)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)

        def _file_handler(filename: str, handler_level: int) -> logging.Handler:
            handler = logging.handlers.RotatingFileHandler(
                directory / filename,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            handler.addFilter(redaction)
            handler.addFilter(context)
            handler.setLevel(handler_level)
            setattr(handler, _OWNED_ATTR, True)
            return handler

        root.addHandler(_file_handler("application.log", resolved_level))
        root.addHandler(_file_handler("errors.log", logging.WARNING))

        decisions = logging.getLogger(DECISION_LOGGER_NAME)
        decisions.addHandler(_file_handler("decisions.log", logging.INFO))
        decisions.propagate = True

        broker = logging.getLogger(BROKER_LOGGER_NAME)
        broker.addHandler(_file_handler("broker.log", logging.INFO))
        broker.propagate = True

    # Third-party loggers: keep them, but quieten the chatty ones.
    for noisy in ("asyncio", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(resolved_level, logging.WARNING))

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return an application logger under the ``ats`` namespace."""
    if name.startswith(ROOT_LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def decision_logger() -> logging.Logger:
    """Logger for trading decisions (separate stream, LOG-002)."""
    return logging.getLogger(DECISION_LOGGER_NAME)


def broker_logger() -> logging.Logger:
    """Logger for broker request/response traffic (separate stream, LOG-004)."""
    return logging.getLogger(BROKER_LOGGER_NAME)
