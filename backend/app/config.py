"""Typed application configuration — ARCH-009, SEC-001, SEC-003, SEC-009,
LIVE-010, LOG-005, DEPLOY-005.

Every setting in the system lives here. ``os.getenv`` is prohibited elsewhere
(the layering test asserts it), so there is exactly one place to look when asking
"what is this system configured to do".

Two classes of check are deliberately kept apart:

* **Configuration contradictions** are fatal at import/boot — e.g. ``LIVE`` mode
  pointed at the paper broker. These cannot be fixed at runtime and must never
  start.
* **Operational preconditions** — capital configured, credentials valid,
  connectivity — are *health checks*, not config errors, because they can become
  true or false while the process runs. See ``app.monitoring``.
"""

from __future__ import annotations

import logging
import os
from datetime import time
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

from app.core.errors import ConfigurationError
from app.modes import TradingMode, parse_trading_mode

logger = logging.getLogger(__name__)

__all__ = [
    "Environment",
    "BrokerProviderName",
    "LLMProviderName",
    "Settings",
    "get_settings",
    "reload_settings",
]

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

HISTORICAL_EXECUTION_SETTINGS = (
    "min_backtest_days",
    "paper_cycle_seconds",
    "paper_entry_max_age_seconds",
    "paper_broker_rejection_limit",
    "tick_staleness_seconds",
    "intraday_squareoff_time",
    "entry_blackout_open_minutes",
    "entry_blackout_close_minutes",
    "max_trades_per_day",
    "allow_positional",
    "loss_cooloff_minutes",
    "max_slippage_pct",
)


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"

    @property
    def is_development(self) -> bool:
        return self is Environment.DEVELOPMENT


class BrokerProviderName(str, Enum):
    PAPER = "paper"
    GROWW = "groww"


class LLMProviderName(str, Enum):
    CLAUDE = "claude"
    OPENAI = "openai"
    FALLBACK = "fallback"


class Settings(BaseSettings):
    """The complete configuration surface of the system.

    Every field here must also appear in ``.env.example`` — enforced by
    ``test_env_example_covers_settings`` (SEC-001).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ------------------------------------------------------
    app_name: str = "ATS"
    environment: Environment = Environment.DEVELOPMENT
    version: str = "0.1.0"
    git_commit: str = "unknown"

    # --- Mode and providers ----------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    broker_provider: BrokerProviderName = BrokerProviderName.PAPER
    llm_provider: LLMProviderName = LLMProviderName.CLAUDE

    # --- API --------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # NoDecode: the value is a plain comma-separated string, not JSON, so
    # pydantic-settings must hand it to the validator untouched.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    tls_enabled: bool = False

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://ats:ats@localhost:5432/ats"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_connect_timeout_seconds: float = Field(default=5, gt=0, le=120)
    database_command_timeout_seconds: float = Field(default=10, gt=0, le=120)
    database_pool_timeout_seconds: float = Field(default=5, gt=0, le=120)
    database_session_timeout_seconds: float = Field(default=20, gt=0, le=120)
    database_echo: bool = False

    # --- Redis ------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_lock_ttl_seconds: int = 60

    # --- Logging ----------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "plain"] = "json"
    log_dir: Optional[Path] = Path("logs")
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 10

    # --- Capital and risk limits (owner-approved defaults) ----------------
    starting_capital: Optional[Decimal] = None
    per_trade_risk_pct: Decimal = Decimal("0.5")
    daily_loss_limit_pct: Decimal = Decimal("2")
    max_drawdown_pct: Decimal = Decimal("10")
    max_gross_exposure_multiple: Decimal = Decimal("3")
    max_concurrent_positions: int = 5
    max_trades_per_day: int = 10
    min_reward_risk_ratio: Decimal = Decimal("1.5")
    margin_buffer_pct: Decimal = Decimal("20")
    max_slippage_pct: Decimal = Decimal("0.3")
    allow_naked_short_options: bool = False
    allow_positional: bool = False
    loss_cooloff_minutes: int = Field(default=30, ge=0)

    # --- Session windows (IST) -------------------------------------------
    entry_blackout_open_minutes: int = Field(default=5, ge=0, le=1440)
    entry_blackout_close_minutes: int = Field(default=20, ge=0, le=1440)
    intraday_squareoff_time: str = "15:10"
    fno_expiry_entry_cutoff_time: str = "14:30"

    # --- Market data ------------------------------------------------------
    tick_staleness_seconds: int = 15
    paper_worker_enabled: bool = False
    instrument_refresh_enabled: bool = False
    paper_cycle_seconds: int = Field(default=5, ge=1, le=60)
    paper_entry_max_age_seconds: int = Field(default=300, ge=1, le=86400)
    paper_broker_rejection_limit: int = Field(default=3, ge=1, le=100)
    historical_plans_file: Path | None = None
    greeks_staleness_seconds: int = 60
    tick_persistence_enabled: bool = True
    tick_sample_interval_seconds: int = 5
    feed_subscription_budget: int = 1000

    # --- Groww broker -----------------------------------------------------
    groww_base_url: str = "https://api.groww.in/v1"
    groww_api_version: str = "1.0"
    groww_api_key: Optional[SecretStr] = None
    groww_api_secret: Optional[SecretStr] = None
    groww_totp_token: Optional[SecretStr] = None
    groww_totp_secret: Optional[SecretStr] = None
    groww_timeout_seconds: float = 10.0
    groww_max_retries: int = 3
    groww_daily_token_budget: int = 150

    # --- Compliance -------------------------------------------------------
    compliance_algo_id: Optional[str] = None
    compliance_algo_registered: bool = False
    compliance_max_orders_per_second: Decimal = Decimal("5")

    # --- LLM --------------------------------------------------------------
    anthropic_api_key: Optional[SecretStr] = None
    llm_model: str = "claude-sonnet-5"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_max_output_tokens: int = Field(default=2048, gt=0, le=128000)
    llm_daily_cost_cap_usd: Decimal = Field(default=Decimal("5"), gt=0, le=100000)
    llm_max_repair_attempts: int = Field(default=1, ge=0, le=1)
    llm_reference_review_enabled: bool = False
    llm_reference_task: Literal["review", "proposal"] = "review"
    llm_daily_token_cap: int = Field(default=2000000, gt=0, le=1000000000000)
    llm_input_usd_per_million: Optional[Decimal] = Field(default=None, gt=0, le=10000)
    llm_output_usd_per_million: Optional[Decimal] = Field(default=None, gt=0, le=10000)
    llm_tariff_model: Optional[str] = None
    llm_tariff_valid_until: Optional[str] = None
    llm_circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    llm_circuit_cooldown_seconds: int = Field(default=60, ge=1, le=3600)

    # --- News -------------------------------------------------------------
    news_enabled: bool = True
    news_polling_enabled: bool = False
    news_poll_batch_size: int = Field(default=5, ge=1, le=20)
    news_research_enabled: bool = False
    news_research_batch_size: int = Field(default=2, ge=1, le=10)
    news_research_max_owner_retries: int = Field(default=1, ge=0, le=3)
    news_critical: bool = False
    news_poll_interval_seconds: int = 300
    news_max_age_hours: int = 24

    # --- Notifications ----------------------------------------------------
    telegram_bot_token: Optional[SecretStr] = None
    telegram_chat_id: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None
    notification_enabled: bool = False
    notification_routes: dict[str, tuple[Literal["INFO", "WARNING", "CRITICAL"], ...]] = Field(
        default_factory=lambda: {"telegram": ("CRITICAL",), "email": ("CRITICAL",)}
    )
    notification_quiet_start: str | None = None
    notification_quiet_end: str | None = None
    notification_queue_size: int = Field(default=100, ge=1, le=10000)
    notification_max_attempts: int = Field(default=3, ge=1, le=5)
    notification_timeout_seconds: float = Field(default=10, gt=0, le=60)
    notification_retry_delay_seconds: float = Field(default=1, ge=0, le=60)
    notification_window_seconds: float = Field(default=60, gt=0, le=86400)
    notification_max_events_per_severity: int = Field(default=100, ge=1, le=10000)
    notification_max_conditions: int = Field(default=1000, ge=1, le=10000)

    # --- Dashboard security ----------------------------------------------
    jwt_secret: Optional[SecretStr] = None
    jwt_expiry_minutes: int = Field(default=60, ge=1, le=1440)
    jwt_refresh_hours: int = Field(default=24, ge=1, le=168)
    dashboard_username: str = "owner"
    dashboard_password_hash: Optional[str] = None
    login_max_attempts: int = Field(default=5, ge=1, le=20)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    # --- Monitoring -------------------------------------------------------
    healthcheck_interval_seconds: int = 60
    clock_max_skew_seconds: float = 2.0
    heartbeat_interval_seconds: int = 30

    # --- Validation gates -------------------------------------------------
    min_paper_sessions_for_live: int = 20
    min_paper_trades_for_live: int = 30
    min_oos_trades_for_live: int = 30
    min_backtest_days: int = Field(default=60, gt=0)

    # --- Validators -------------------------------------------------------

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _parse_mode(cls, value: Any) -> TradingMode:
        """ARCH-003: never raise; unparseable values fall back to PAPER."""
        if isinstance(value, TradingMode):
            return value
        return parse_trading_mode(None if value is None else str(value))

    @field_validator("broker_provider", "llm_provider", "environment", mode="before")
    @classmethod
    def _normalise_enum(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return value
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            candidate = value.strip().upper()
            if candidate not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise ValueError(
                    f"log_level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {value!r}"
                )
            return candidate
        return value

    @field_validator(
        "per_trade_risk_pct",
        "daily_loss_limit_pct",
        "max_drawdown_pct",
        "margin_buffer_pct",
        "max_slippage_pct",
    )
    @classmethod
    def _percent_range(cls, value: Decimal) -> Decimal:
        if not (Decimal(0) < value <= Decimal(100)):
            raise ValueError(f"percentage must be in (0, 100], got {value}")
        return value

    @field_validator("historical_plans_file", mode="before")
    @classmethod
    def _blank_historical_plans(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("starting_capital", mode="before")
    @classmethod
    def _missing_capital(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "llm_input_usd_per_million", "llm_output_usd_per_million",
        "llm_tariff_model", "llm_tariff_valid_until", mode="before",
    )
    @classmethod
    def _missing_llm_tariff(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("intraday_squareoff_time", "fno_expiry_entry_cutoff_time")
    @classmethod
    def _squareoff_time(cls, value):
        parsed = time.fromisoformat(value)
        if len(value) != 5 or parsed.tzinfo is not None or parsed.second or parsed.microsecond:
            raise ValueError("square-off time must be local HH:MM")
        return value

    @field_validator("starting_capital")
    @classmethod
    def _positive_capital(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is not None and value <= 0:
            raise ValueError(f"starting_capital must be positive, got {value}")
        return value

    @field_validator("max_concurrent_positions", "max_trades_per_day")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"must be a positive integer, got {value}")
        return value

    @model_validator(mode="after")
    def _check_coherence(self) -> "Settings":
        self._check_mode_and_broker()
        self._check_network_exposure()
        self._check_cors()
        self._warn_debug_in_live()
        return self

    # --- Coherence checks -------------------------------------------------

    def _check_mode_and_broker(self) -> None:
        """LIVE-010: LIVE mode may not be wired to the paper broker."""
        if self.trading_mode.uses_real_broker and self.broker_provider is BrokerProviderName.PAPER:
            raise ConfigurationError(
                f"TRADING_MODE={self.trading_mode.value} requires a real broker, but "
                f"BROKER_PROVIDER=paper. Set BROKER_PROVIDER=groww, or set "
                f"TRADING_MODE=PAPER.",
                context={
                    "trading_mode": self.trading_mode.value,
                    "broker_provider": self.broker_provider.value,
                },
            )
        if (
            self.trading_mode is TradingMode.PAPER
            and self.broker_provider is BrokerProviderName.GROWW
        ):
            logger.warning(
                "TRADING_MODE=PAPER with BROKER_PROVIDER=groww: the paper broker will be "
                "used for execution; Groww will be used for market data only."
            )

    def _check_network_exposure(self) -> None:
        """SEC-009: a non-local bind requires TLS to be configured explicitly."""
        host = self.api_host.strip()
        if host in _LOCAL_HOSTS:
            return
        if not self.tls_enabled:
            raise ConfigurationError(
                f"API_HOST={host!r} exposes the API beyond localhost but TLS_ENABLED is false. "
                f"Set TLS_ENABLED=true and terminate TLS in front of the app, or bind to "
                f"127.0.0.1 and use an SSH tunnel.",
                context={"api_host": host},
            )

    def _check_cors(self) -> None:
        """SEC-003: wildcard CORS is a development-only convenience."""
        if "*" in self.cors_origins and self.environment is not Environment.DEVELOPMENT:
            raise ConfigurationError(
                f"CORS_ORIGINS contains '*' but ENVIRONMENT={self.environment.value}. "
                f"List the dashboard origin explicitly.",
                context={"environment": self.environment.value},
            )

    def _warn_debug_in_live(self) -> None:
        """LOG-005: DEBUG logging in LIVE is allowed but must be noticed."""
        if self.trading_mode is TradingMode.LIVE and self.log_level == "DEBUG":
            logger.warning(
                "LOG_LEVEL=DEBUG while TRADING_MODE=LIVE. Debug logging is verbose and may "
                "slow the trading loop; it also increases the volume of data written to disk."
            )

    # --- Derived values ---------------------------------------------------

    @property
    def has_groww_credentials(self) -> bool:
        """True when at least one complete Groww auth flow is configured."""
        key_secret = self.groww_api_key is not None and self.groww_api_secret is not None
        totp = self.groww_totp_token is not None and self.groww_totp_secret is not None
        return key_secret or totp

    @property
    def groww_auth_flow(self) -> Optional[str]:
        """Which auth flow will be used: ``totp`` is preferred (no expiry)."""
        if self.groww_totp_token is not None and self.groww_totp_secret is not None:
            return "totp"
        if self.groww_api_key is not None and self.groww_api_secret is not None:
            return "api_key"
        return None

    @property
    def effective_llm_provider(self) -> LLMProviderName:
        """Resolve the provider actually usable right now.

        Claude is the configured default, but the system must run with no API key
        (LLM-011). Rather than failing to boot, an unusable Claude configuration
        degrades to the deterministic fallback and says so.
        """
        if self.llm_provider is LLMProviderName.CLAUDE and self.anthropic_api_key is None:
            logger.warning(
                "LLM_PROVIDER=claude but ANTHROPIC_API_KEY is not set; using the "
                "deterministic fallback provider. LLM-dependent strategies will stand down."
            )
            return LLMProviderName.FALLBACK
        return self.llm_provider

    @property
    def secret_values(self) -> list[str]:
        """Every configured secret, for registration with the log redactor."""
        values: list[str] = []
        for field_name in type(self).model_fields:
            value = getattr(self, field_name, None)
            if isinstance(value, SecretStr):
                revealed = value.get_secret_value()
                if revealed:
                    values.append(revealed)
        return values

    def redacted_summary(self) -> dict[str, Any]:
        """Effective configuration with secrets masked (DEPLOY-005)."""
        summary: dict[str, Any] = {}
        for field_name in type(self).model_fields:
            value = getattr(self, field_name, None)
            if field_name == "dashboard_password_hash":
                summary[field_name] = "***SET***" if value else None
            elif isinstance(value, SecretStr):
                summary[field_name] = "***SET***" if value.get_secret_value() else None
            elif isinstance(value, Enum):
                summary[field_name] = value.value
            elif isinstance(value, (Decimal, Path)):
                summary[field_name] = str(value)
            else:
                summary[field_name] = value
        summary["effective_llm_provider"] = self.effective_llm_provider.value
        summary["groww_auth_flow"] = self.groww_auth_flow
        summary["has_groww_credentials"] = self.has_groww_credentials
        return summary


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read configuration (tests and explicit reloads)."""
    get_settings.cache_clear()
    return get_settings()


def validate_historical_settings(payload: dict[str, Any]) -> Settings:
    """Validate isolated settings without changing the serving process."""
    required = {"database_url", "starting_capital"}
    if not required <= set(payload) or set(payload) - required - set(HISTORICAL_EXECUTION_SETTINGS):
        raise ValueError("explicit database/capital and permitted execution settings required")
    if any(value is None or isinstance(value, (dict, list, bool)) for value in payload.values()):
        raise ValueError("scalar historical settings required")
    values = payload | {"trading_mode": "PAPER", "broker_provider": "paper"}
    return Settings(_env_file=None, **values)


def configure_historical_process(payload: dict[str, Any]) -> Settings:
    """Install explicit PAPER settings in the dedicated historical child only."""
    validate_historical_settings(payload)
    values = payload | {"trading_mode": "PAPER", "broker_provider": "paper"}
    for key, value in values.items():
        os.environ[key.upper()] = str(value)
    return reload_settings()
