"""Trading mode and configuration.

Covers ARCH-001, ARCH-002, ARCH-003, ARCH-009, SEC-001, SEC-003, SEC-009,
LIVE-010, LOG-005, DEPLOY-005.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import (
    BrokerProviderName,
    Environment,
    LLMProviderName,
    Settings,
    reload_settings,
)
from app.core.errors import ConfigurationError
from app.modes import (
    ImmutableModeError,
    TradingMode,
    current_mode,
    parse_trading_mode,
    reset_mode_for_testing,
    resolve_mode,
)
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit


# --- ARCH-001 -------------------------------------------------------------


def test_modes_enum() -> None:
    assert [m.value for m in TradingMode] == ["PAPER", "SUPERVISED", "LIVE"]

    assert TradingMode.PAPER.uses_real_broker is False
    assert TradingMode.SUPERVISED.uses_real_broker is True
    assert TradingMode.LIVE.uses_real_broker is True

    assert TradingMode.SUPERVISED.requires_human_approval_per_order is True
    assert TradingMode.LIVE.requires_human_approval_per_order is False
    assert TradingMode.LIVE.requires_arming is True
    assert TradingMode.PAPER.requires_arming is False


# --- ARCH-003 -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "live-ish", "REAL", "prod", "1", "papers"],
)
def test_mode_defaults_paper(raw: str | None) -> None:
    # Anything unset or unrecognised must resolve to the mode that cannot lose
    # money, and must never raise (raising would tempt a caller into a fallback).
    assert parse_trading_mode(raw) is TradingMode.PAPER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PAPER", TradingMode.PAPER),
        ("paper", TradingMode.PAPER),
        (" supervised ", TradingMode.SUPERVISED),
        ("LIVE", TradingMode.LIVE),
    ],
)
def test_mode_parsing_accepts_valid_values(raw: str, expected: TradingMode) -> None:
    assert parse_trading_mode(raw) is expected


def test_settings_default_mode_is_paper(settings_env) -> None:
    settings = settings_env(TRADING_MODE=None)
    assert settings.trading_mode is TradingMode.PAPER

    settings = settings_env(TRADING_MODE="nonsense")
    assert settings.trading_mode is TradingMode.PAPER


# --- ARCH-002 -------------------------------------------------------------


def test_mode_immutable() -> None:
    reset_mode_for_testing()
    with pytest.raises(ImmutableModeError):
        current_mode()

    resolve_mode(TradingMode.PAPER)
    assert current_mode() is TradingMode.PAPER

    # Re-resolving the same mode is a harmless no-op (startup and tests both do it).
    assert resolve_mode(TradingMode.PAPER) is TradingMode.PAPER

    with pytest.raises(ImmutableModeError):
        resolve_mode(TradingMode.LIVE)
    assert current_mode() is TradingMode.PAPER


# --- ARCH-009 -------------------------------------------------------------


def test_settings_validation(settings_env) -> None:
    settings = settings_env(
        STARTING_CAPITAL="500000",
        PER_TRADE_RISK_PCT="0.5",
        DAILY_LOSS_LIMIT_PCT="2",
    )
    assert settings.starting_capital == Decimal("500000")
    assert settings.per_trade_risk_pct == Decimal("0.5")
    assert settings.environment is Environment.DEVELOPMENT

    with pytest.raises(Exception):
        settings_env(PER_TRADE_RISK_PCT="0")
    with pytest.raises(Exception):
        settings_env(PER_TRADE_RISK_PCT="150")
    with pytest.raises(Exception):
        settings_env(STARTING_CAPITAL="-1")
    with pytest.raises(Exception):
        settings_env(MAX_CONCURRENT_POSITIONS="0")
    with pytest.raises(Exception):
        settings_env(LOG_LEVEL="CHATTY")


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_blank_capital_remains_unavailable(settings_env, raw) -> None:
    settings = settings_env(STARTING_CAPITAL=raw)
    assert settings.starting_capital is None
    assert settings.trading_mode is TradingMode.PAPER


def test_example_dotenv_loads_without_inventing_capital(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dotenv = tmp_path / ".env"
    dotenv.write_text((REPO_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=dotenv)
    assert settings.starting_capital is None
    assert settings.trading_mode is TradingMode.PAPER


@pytest.mark.parametrize("raw", ["0", "-1", "invalid", "NaN", "Infinity"])
def test_invalid_capital_still_rejected(settings_env, raw) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings_env(STARTING_CAPITAL=raw)


def test_owner_approved_defaults_are_the_defaults(settings_env) -> None:
    settings = settings_env()
    assert settings.per_trade_risk_pct == Decimal("0.5")
    assert settings.daily_loss_limit_pct == Decimal("2")
    assert settings.max_drawdown_pct == Decimal("10")
    assert settings.max_gross_exposure_multiple == Decimal("3")
    assert settings.max_concurrent_positions == 5
    assert settings.allow_naked_short_options is False
    assert settings.allow_positional is False
    assert settings.news_critical is False
    assert settings.trading_mode is TradingMode.PAPER
    assert settings.broker_provider is BrokerProviderName.PAPER
    assert settings.llm_provider is LLMProviderName.CLAUDE
    # Capital has no default on purpose: an invented number is invented risk.
    assert settings.starting_capital is None


def test_cors_origins_accept_comma_separated(settings_env) -> None:
    settings = settings_env(CORS_ORIGINS="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


# --- LIVE-010 -------------------------------------------------------------


def test_live_config_consistency(settings_env) -> None:
    # LIVE pointed at the paper broker is a contradiction: refuse to start.
    with pytest.raises(ConfigurationError) as excinfo:
        settings_env(TRADING_MODE="LIVE", BROKER_PROVIDER="paper")
    assert "BROKER_PROVIDER" in str(excinfo.value)

    with pytest.raises(ConfigurationError):
        settings_env(TRADING_MODE="SUPERVISED", BROKER_PROVIDER="paper")

    settings = settings_env(TRADING_MODE="LIVE", BROKER_PROVIDER="groww")
    assert settings.trading_mode is TradingMode.LIVE

    # PAPER with the Groww provider is legal: Groww is then a data source only.
    settings = settings_env(TRADING_MODE="PAPER", BROKER_PROVIDER="groww")
    assert settings.trading_mode is TradingMode.PAPER


# --- SEC-003 / SEC-009 ----------------------------------------------------


def test_cors_policy(settings_env) -> None:
    settings = settings_env(CORS_ORIGINS="*", ENVIRONMENT="development")
    assert settings.cors_origins == ["*"]

    with pytest.raises(ConfigurationError):
        settings_env(CORS_ORIGINS="*", ENVIRONMENT="production")


def test_bind_policy(settings_env) -> None:
    assert settings_env(API_HOST="127.0.0.1").api_host == "127.0.0.1"
    assert settings_env(API_HOST="localhost").api_host == "localhost"

    with pytest.raises(ConfigurationError) as excinfo:
        settings_env(API_HOST="0.0.0.0", TLS_ENABLED="false")
    assert "TLS_ENABLED" in str(excinfo.value)

    settings = settings_env(API_HOST="0.0.0.0", TLS_ENABLED="true")
    assert settings.api_host == "0.0.0.0"


# --- LLM provider degradation --------------------------------------------


def test_claude_without_key_degrades_to_fallback(settings_env) -> None:
    settings = settings_env(LLM_PROVIDER="claude", ANTHROPIC_API_KEY=None)
    assert settings.llm_provider is LLMProviderName.CLAUDE
    # The system must run with no API key, so the effective provider degrades.
    assert settings.effective_llm_provider is LLMProviderName.FALLBACK

    settings = settings_env(LLM_PROVIDER="claude", ANTHROPIC_API_KEY="sk-ant-test-key")
    assert settings.effective_llm_provider is LLMProviderName.CLAUDE


# --- SEC-001 / DEPLOY-005 -------------------------------------------------


def test_env_example_covers_settings() -> None:
    """Every Settings field must be documented in .env.example."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z0-9_]+)=", example, flags=re.MULTILINE))
    expected = {name.upper() for name in Settings.model_fields}
    missing = sorted(expected - documented)
    assert not missing, f"undocumented settings in .env.example: {missing}"


def test_gitignore_excludes_env_file() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.env$", gitignore, flags=re.MULTILINE)
    assert re.search(r"^!\.env\.example$", gitignore, flags=re.MULTILINE)


def test_boot_config_summary_redacts_secrets(settings_env) -> None:
    settings = settings_env(
        GROWW_API_KEY="super-secret-key",
        ANTHROPIC_API_KEY="sk-ant-secret",
        STARTING_CAPITAL="250000",
    )
    summary = settings.redacted_summary()

    serialised = str(summary)
    assert "super-secret-key" not in serialised
    assert "sk-ant-secret" not in serialised
    assert summary["groww_api_key"] == "***SET***"
    assert summary["anthropic_api_key"] == "***SET***"
    assert summary["groww_api_secret"] is None
    assert summary["trading_mode"] == "PAPER"
    assert summary["starting_capital"] == "250000"
    assert summary["effective_llm_provider"] == "claude"


def test_secret_values_collects_every_configured_secret(settings_env) -> None:
    settings = settings_env(GROWW_TOTP_SECRET="totp-secret", JWT_SECRET="jwt-secret")
    values = settings.secret_values
    assert "totp-secret" in values
    assert "jwt-secret" in values


def test_groww_auth_flow_prefers_totp(settings_env) -> None:
    assert settings_env().groww_auth_flow is None
    assert not settings_env().has_groww_credentials

    api_flow = settings_env(GROWW_API_KEY="k", GROWW_API_SECRET="s")
    assert api_flow.groww_auth_flow == "api_key"
    assert api_flow.has_groww_credentials

    # TOTP tokens do not expire, so they are preferred when both are present.
    both = settings_env(
        GROWW_API_KEY="k",
        GROWW_API_SECRET="s",
        GROWW_TOTP_TOKEN="t",
        GROWW_TOTP_SECRET="ts",
    )
    assert both.groww_auth_flow == "totp"


def test_log_level_policy_warns_on_debug_in_live(settings_env, caplog) -> None:
    with caplog.at_level("WARNING"):
        settings_env(
            TRADING_MODE="LIVE", BROKER_PROVIDER="groww", LOG_LEVEL="DEBUG"
        )
    assert any("DEBUG" in record.message for record in caplog.records)


def test_settings_are_cached_until_reloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    first = get_settings()
    assert get_settings() is first

    monkeypatch.setenv("APP_NAME", "Renamed")
    assert get_settings() is first  # still cached
    assert reload_settings().app_name == "Renamed"
