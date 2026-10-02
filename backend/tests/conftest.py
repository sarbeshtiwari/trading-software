"""Shared test fixtures — TEST-001.

Principles:

* Unit tests touch no external service. The database fixture uses SQLite via
  ``aiosqlite``; PostgreSQL-specific behaviour is covered by the integration
  suite, which is marked and skipped when no server is configured.
* Time is always injected. Nothing in the suite depends on the wall clock.
* Process-global state (resolved mode, health registry, trading gate, settings
  cache) is reset between tests, so ordering can never make a test pass.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from app.core import calendar as calendar_module
from app.core.clock import IST, FakeClock, reset_clock, set_clock

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: Every setting the suite controls. Cleared before each test so a stray value
#: from the developer's environment cannot change behaviour under test.
_MANAGED_ENV_PREFIXES = (
    "HISTORICAL_PLANS_FILE",
    "TRADING_MODE",
    "BROKER_PROVIDER",
    "LLM_",
    "ENVIRONMENT",
    "API_HOST",
    "API_PORT",
    "CORS_ORIGINS",
    "TLS_ENABLED",
    "DATABASE_URL",
    "REDIS_URL",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "LOG_DIR",
    "STARTING_CAPITAL",
    "PER_TRADE_RISK_PCT",
    "DAILY_LOSS_LIMIT_PCT",
    "MAX_DRAWDOWN_PCT",
    "GROWW_",
    "ANTHROPIC_API_KEY",
    "NEWS_",
    "COMPLIANCE_",
    "JWT_SECRET",
    "JWT_",
    "DASHBOARD_",
    "LOGIN_",
    "TELEGRAM_",
    "SMTP_",
    "NOTIFICATION_",
    "PAPER_",
    "INSTRUMENT_",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key in list(os.environ):
        if any(key.startswith(prefix) for prefix in _MANAGED_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)

    # Point settings at an isolated, non-existent .env so the developer's real
    # configuration can never leak into a test.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("REDIS_URL", "memory://test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    yield


@pytest.fixture(autouse=True)
def _reset_global_state() -> Iterator[None]:
    """Reset every process-global the application keeps."""
    from app.config import get_settings
    from app.core.lifecycle import reset_lifecycle
    from app.core.logging import clear_secrets
    from app.modes import reset_mode_for_testing
    from app.monitoring.gate import reset_trading_gate
    from app.monitoring.healthchecks import reset_health_registry

    def _reset() -> None:
        reset_mode_for_testing()
        reset_health_registry()
        reset_trading_gate()
        reset_lifecycle()
        clear_secrets()
        # Only drop the cache; constructing Settings here would re-read whatever
        # environment the test left behind and could legitimately raise (a test
        # that asserts a bad configuration is rejected would then fail in teardown).
        get_settings.cache_clear()
        # The instrument index is process-global; a leftover index would let
        # one test satisfy another test's instruments check.
        import app.instruments.resolver as resolver_module

        resolver_module._resolver = None
        calendar_module._calendar = None

    _reset()
    yield
    _reset()


@pytest.fixture
def authenticated_api(monkeypatch):
    from app.security.auth import Principal

    async def authenticated(request):
        return Principal(username="test-owner", session_id="test-session")

    monkeypatch.setattr("app.api.deps.authenticate_request", authenticated)


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    """Deterministic clock fixed at 2026-01-05 09:30 IST (a Monday, mid-session)."""
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 30, tzinfo=IST))
    set_clock(clock)
    try:
        yield clock
    finally:
        reset_clock()


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch):
    """Set environment variables and return freshly loaded settings."""
    from app.config import reload_settings

    def _apply(**values: object):
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key.upper(), raising=False)
            else:
                monkeypatch.setenv(key.upper(), str(value))
        return reload_settings()

    return _apply


@pytest_asyncio.fixture
async def db_engine(tmp_path: Path) -> AsyncIterator[object]:
    """SQLite engine with the full schema created from the models."""
    from app.db import session as db_session
    from app.db.models import metadata

    engine = db_session.init_engine(force=True)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        await db_session.dispose_engine()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):  # type: ignore[no-untyped-def]
    from app.db.session import get_sessionmaker

    return get_sessionmaker()
