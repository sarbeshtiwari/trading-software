"""Startup health sequence — MON-002, MON-004, DEPLOY-004.

Runs every registered check, updates the trading gate from the result, and
reports **coverage against the contract §10 list** so that a check which does not
yet exist is visible as missing rather than quietly absent.

That coverage report is the honest answer to "are all thirteen startup checks
implemented?" at any point during the build.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.monitoring.checks import _expected_startup_checks, register_core_checks
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthReport, get_health_registry

logger = get_logger("monitoring.startup")

__all__ = ["StartupResult", "run_startup_checks", "startup_check_coverage"]


@dataclass(frozen=True)
class StartupResult:
    report: HealthReport
    trading_allowed: bool
    blocking_reason: Optional[str]
    missing_checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.report.to_dict(),
            "trading_allowed": self.trading_allowed,
            "blocking_reason": self.blocking_reason,
            "missing_checks": list(self.missing_checks),
        }


def startup_check_coverage() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(present, missing)`` against the contract §10 list."""
    registered = set(get_health_registry().names)
    expected = _expected_startup_checks()
    present = tuple(name for name in expected if name in registered)
    missing = tuple(name for name in expected if name not in registered)
    return present, missing


def _safe_broker(settings: Settings):  # type: ignore[no-untyped-def]
    """Build the configured broker, or None if it cannot be constructed.

    A construction failure is itself reported by the checks (which then skip or
    fail), so it must not prevent the health run from happening at all.
    """
    try:
        from app.brokers.factory import create_broker_provider
        from app.brokers.registry import register_all

        register_all()
        return create_broker_provider(settings)
    except Exception as exc:  # noqa: BLE001 - reported through the checks
        logger.warning("Broker provider unavailable for health checks",
                       extra={"error": str(exc)})
        return None


def _safe_market_data(settings: Settings):  # type: ignore[no-untyped-def]
    if not settings.trading_mode.uses_real_broker:
        return None
    try:
        if not settings.has_groww_credentials:
            return None
        from app.marketdata.factory import (
            MarketDataProviderName,
            create_market_data_provider,
        )
        import app.marketdata.live  # noqa: F401 - registers the provider

        return create_market_data_provider(settings, name=MarketDataProviderName.LIVE)
    except Exception as exc:  # noqa: BLE001 - reported through the checks
        logger.warning("Market-data provider unavailable for health checks",
                       extra={"error": str(exc)})
        return None


async def run_startup_checks(settings: Optional[Settings] = None) -> StartupResult:
    """Register core checks, run everything, and set the trading gate."""
    resolved = settings or get_settings()
    registry = get_health_registry()
    if not registry.names:
        register_core_checks(
            resolved,
            broker=_safe_broker(resolved),
            market_data=_safe_market_data(resolved),
        )

    report = await registry.run_all()
    gate = get_trading_gate()

    if report.trading_allowed:
        gate.clear("health")
        gate.clear("startup")
    else:
        gate.block("health", report.blocking_reason() or "critical health check failed",
                   blocks_exits=False)
        gate.clear("startup")

    present, missing = startup_check_coverage()
    if missing:
        # Not an error during the build — but it must never be silent.
        logger.warning(
            "Startup checks from the specification are not yet implemented",
            extra={"present": list(present), "missing": list(missing)},
        )

    logger.info(
        "Startup health checks complete",
        extra={
            "status": report.overall_status.value,
            "trading_allowed": report.trading_allowed and gate.new_entries_allowed,
            "checks_run": len(report.results),
        },
    )

    return StartupResult(
        report=report,
        trading_allowed=report.trading_allowed and gate.new_entries_allowed,
        blocking_reason=report.blocking_reason() or gate.reason(),
        missing_checks=missing,
    )
