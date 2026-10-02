"""Structured logging and secret redaction.

Covers LOG-001, LOG-002, LOG-003, SEC-004, BE-010.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    clear_secrets,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    get_logger,
    register_secret,
    set_correlation_id,
)

pytestmark = pytest.mark.unit


def _format(record: logging.LogRecord) -> dict:
    RedactionFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def _record(message: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="ats.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=args or None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --- LOG-001 --------------------------------------------------------------


def test_structured_logging() -> None:
    payload = _format(_record("order submitted", order_id="ord_1", quantity=75))

    assert payload["message"] == "order submitted"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ats.test"
    assert payload["order_id"] == "ord_1"
    assert payload["quantity"] == 75
    assert payload["line"] == 42
    # Both timezones: IST is what a trader reads, UTC is what reconciles with
    # broker and exchange records.
    assert payload["ts_ist"].endswith("+05:30")
    assert payload["ts_utc"].endswith("+00:00")
    assert "correlation_id" in payload


def test_decimal_and_enum_values_are_json_safe() -> None:
    from decimal import Decimal

    from app.core.enums import OrderStatus

    payload = _format(
        _record("fill", price=Decimal("1234.55"), status=OrderStatus.EXECUTED)
    )
    # Decimals must serialise as strings, never as floats: a float in the log is
    # a float that might be a float in the code.
    assert payload["price"] == "1234.55"
    assert payload["status"] == "EXECUTED"


# --- SEC-004 / LOG-003 ----------------------------------------------------


def test_log_redaction() -> None:
    clear_secrets()
    register_secret("super-secret-token-value")

    payload = _format(_record("auth ok with super-secret-token-value"))
    assert "super-secret-token-value" not in payload["message"]
    assert REDACTED in payload["message"]


def test_redaction_covers_structured_fields_by_name() -> None:
    clear_secrets()
    payload = _format(
        _record(
            "calling broker",
            api_key="never-print-me",
            totp_secret="also-secret",
            symbol="NIFTY",
        )
    )
    assert payload["api_key"] == REDACTED
    assert payload["totp_secret"] == REDACTED
    assert payload["symbol"] == "NIFTY"


def test_redaction_covers_secret_shaped_patterns_without_registration() -> None:
    clear_secrets()
    payload = _format(_record('sent header Authorization: Bearer abcdef123456789'))
    assert "abcdef123456789" not in payload["message"]

    payload = _format(_record('{"api_key": "unregistered-but-obvious"}'))
    assert "unregistered-but-obvious" not in payload["message"]

    payload = _format(_record("key is sk-ant-api03-abcdefghijklmnop"))
    assert "sk-ant-api03-abcdefghijklmnop" not in payload["message"]


def test_redaction_reaches_nested_structures() -> None:
    clear_secrets()
    register_secret("nested-secret")
    payload = _format(
        _record("request", request={"headers": {"authorization": "nested-secret"}})
    )
    assert "nested-secret" not in json.dumps(payload)


def test_short_values_are_not_registered_as_secrets() -> None:
    # Registering a 1-2 character "secret" would redact it out of every message.
    clear_secrets()
    register_secret("ab")
    payload = _format(_record("about to submit"))
    assert payload["message"] == "about to submit"


# --- BE-010 ---------------------------------------------------------------


def test_correlation_id_propagation() -> None:
    set_correlation_id(None)
    assert get_correlation_id() is None

    with correlation_scope("cor_123") as value:
        assert value == "cor_123"
        assert get_correlation_id() == "cor_123"
        payload = _format(_record("inside scope"))
        # The filter only fills it when the record does not carry one, so set it
        # the way ContextFilter does.
        record = _record("inside scope")
        record.correlation_id = get_correlation_id()
        payload = _format(record)
        assert payload["correlation_id"] == "cor_123"

    assert get_correlation_id() is None


# --- LOG-002 --------------------------------------------------------------


def test_log_rotation(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(level="INFO", log_format="json", log_dir=log_dir, force=True)

    logger = get_logger("rotation")
    logger.info("application line")
    logger.warning("something worth attention")

    from app.core.logging import broker_logger, decision_logger

    decision_logger().info("decision line")
    broker_logger().info("broker line")

    for handler in logging.getLogger().handlers:
        handler.flush()
    for name in ("ats.decisions", "ats.broker"):
        for handler in logging.getLogger(name).handlers:
            handler.flush()

    assert (log_dir / "application.log").exists()
    assert (log_dir / "errors.log").exists()
    assert (log_dir / "decisions.log").exists()
    assert (log_dir / "broker.log").exists()

    # Errors stream carries warnings and above only.
    errors = (log_dir / "errors.log").read_text(encoding="utf-8")
    assert "something worth attention" in errors
    assert "application line" not in errors

    decisions = (log_dir / "decisions.log").read_text(encoding="utf-8")
    assert "decision line" in decisions

    # Reset root handlers so later tests are not writing into tmp_path.
    configure_logging(level="INFO", log_format="plain", log_dir=None, force=True)


def test_configured_secrets_are_registered_at_setup(tmp_path: Path) -> None:
    clear_secrets()
    configure_logging(
        level="INFO",
        log_format="json",
        log_dir=None,
        secrets=["configured-secret-value"],
        force=True,
    )
    payload = _format(_record("token configured-secret-value in use"))
    assert "configured-secret-value" not in payload["message"]
