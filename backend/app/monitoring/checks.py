"""Concrete health checks available at P0.

The contract §10 list has thirteen entries. The ones implemented here are those
whose subjects exist at this phase: database, Redis, system clock, risk
configuration, mode coherence and trading state. Market data, Groww
authentication, account/margin/positions, instruments, news, order service and
market status arrive with the phases that build them, and each registers its own
check. ``startup.py`` reports which of the thirteen are present, so a missing
check is visible rather than silently absent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import sqlalchemy as sa

from app.config import Settings, get_settings
from app.core.clock import get_clock, to_utc
from app.core.enums import HealthStatus
from app.core.locks import create_lock_store
from app.db import session as db_session
from app.modes import current_mode
from app.monitoring.healthchecks import HealthCheck

__all__ = [
    "DatabaseCheck",
    "RedisCheck",
    "SystemClockCheck",
    "RiskConfigCheck",
    "ModeCoherenceCheck",
    "register_core_checks",
]


class DatabaseCheck(HealthCheck):
    """Connectivity plus schema presence. Critical: nothing works without it."""

    name = "database"
    critical = True
    timeout_seconds = 5.0

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        engine = db_session.get_engine()
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
            # A reachable database with no schema is still unusable for trading.
            has_table = await connection.run_sync(
                lambda sync_conn: sa.inspect(sync_conn).has_table("system_state")
            )
        if not has_table:
            return (
                HealthStatus.FAIL,
                "connected, but the schema is missing — run `alembic upgrade head`",
                {"dialect": engine.dialect.name},
            )
        return HealthStatus.PASS, "connected", {"dialect": engine.dialect.name}


class RedisCheck(HealthCheck):
    """Redis reachability.

    Critical whenever a real broker can be reached: the exclusive trading lock
    lives there, and without it two processes could trade the same account. In
    PAPER mode on a single machine, an in-process store is acceptable and this
    check reports ``DEGRADED`` rather than failing.
    """

    name = "redis"
    critical = True
    timeout_seconds = 5.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self.critical = self._settings.trading_mode.uses_real_broker

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        url = self._settings.redis_url
        if url.startswith("memory://"):
            if self._settings.trading_mode.uses_real_broker:
                return (
                    HealthStatus.FAIL,
                    "REDIS_URL=memory:// cannot provide a cross-process lock in "
                    f"{self._settings.trading_mode.value} mode",
                    {"redis_url": url},
                )
            return (
                HealthStatus.DEGRADED,
                "using the in-process store; safe for a single-process PAPER run only",
                {"redis_url": url},
            )

        store = create_lock_store(url)
        try:
            token = "healthcheck"
            key = "ats:health:probe"
            acquired = await store.acquire(key, token, 1000)
            if acquired:
                await store.release(key, token)
            return HealthStatus.PASS, "connected", {"redis_url": _redact_url(url)}
        finally:
            await store.close()


class SystemClockCheck(HealthCheck):
    """Clock skew against the database server's clock — MON-003.

    Session boundaries, expiry, square-off cutoffs and staleness all depend on
    local time being right. The database is used as the reference because it is
    the one external clock guaranteed to be reachable at startup.
    """

    name = "system_clock"
    critical = True
    timeout_seconds = 5.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        engine = db_session.get_engine()
        if engine.dialect.name == "sqlite":
            return (
                HealthStatus.SKIPPED,
                "no external clock reference available on sqlite",
                {},
            )

        async with engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT now()"))
            reference = result.scalar_one()

        local = get_clock().utcnow()
        skew = abs((to_utc(reference) - local).total_seconds())
        limit = self._settings.clock_max_skew_seconds
        context = {"skew_seconds": round(skew, 3), "limit_seconds": limit}

        if skew > limit:
            return (
                HealthStatus.FAIL,
                f"local clock differs from the database clock by {skew:.3f}s "
                f"(limit {limit}s)",
                context,
            )
        return HealthStatus.PASS, f"skew {skew:.3f}s", context


class RiskConfigCheck(HealthCheck):
    """Risk limits must be configured before any trading, in any mode.

    Capital has no default on purpose: sizing, the daily loss limit and the
    drawdown limit are all expressed against it, so an invented number would
    produce invented risk.
    """

    name = "risk_config"
    critical = True
    timeout_seconds = 2.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        settings = self._settings
        if settings.starting_capital is None:
            return (
                HealthStatus.FAIL,
                "STARTING_CAPITAL is not configured; position sizing and the daily loss "
                "limit cannot be computed",
                {},
            )
        context = {
            "capital": str(settings.starting_capital),
            "per_trade_risk_pct": str(settings.per_trade_risk_pct),
            "daily_loss_limit_pct": str(settings.daily_loss_limit_pct),
            "max_drawdown_pct": str(settings.max_drawdown_pct),
        }
        if settings.per_trade_risk_pct > settings.daily_loss_limit_pct:
            return (
                HealthStatus.FAIL,
                "PER_TRADE_RISK_PCT exceeds DAILY_LOSS_LIMIT_PCT: a single losing trade "
                "would breach the daily limit",
                context,
            )
        return HealthStatus.PASS, "risk limits configured", context


class ModeCoherenceCheck(HealthCheck):
    """The resolved mode, the configured broker and the process agree."""

    name = "mode"
    critical = True
    timeout_seconds = 2.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        settings = self._settings
        resolved = current_mode()
        context = {
            "resolved_mode": resolved.value,
            "configured_mode": settings.trading_mode.value,
            "broker_provider": settings.broker_provider.value,
        }
        if resolved is not settings.trading_mode:
            return (
                HealthStatus.FAIL,
                f"process mode {resolved.value} does not match configured "
                f"{settings.trading_mode.value}",
                context,
            )
        if resolved.uses_real_broker and not settings.has_groww_credentials:
            return (
                HealthStatus.FAIL,
                f"{resolved.value} mode requires Groww credentials; none are configured",
                context,
            )
        return HealthStatus.PASS, f"mode {resolved.value}", context


def _redact_url(url: str) -> str:
    """Strip any credentials from a connection URL before logging it."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


def register_core_checks(
    settings: Optional[Settings] = None,
    *,
    broker: Optional[Any] = None,
    market_data: Optional[Any] = None,
) -> None:
    """Register every check whose subject exists at this phase."""
    from app.monitoring.broker_checks import register_broker_checks
    from app.monitoring.healthchecks import get_health_registry

    resolved = settings or get_settings()
    registry = get_health_registry()
    registry.register(DatabaseCheck())
    registry.register(RedisCheck(resolved))
    registry.register(SystemClockCheck(resolved))
    registry.register(RiskConfigCheck(resolved))
    registry.register(ModeCoherenceCheck(resolved))

    register_broker_checks(broker=broker, market_data=market_data, settings=resolved)


def _expected_startup_checks() -> tuple[str, ...]:
    """The contract §10 list, in order. Used by the coverage report."""
    return (
        "database",
        "redis",
        "market_data",
        "groww_auth",
        "account",
        "margin",
        "positions",
        "instruments",
        "news",
        "risk_config",
        "order_service",
        "system_clock",
        "market_status",
    )
