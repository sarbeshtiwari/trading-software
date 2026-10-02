"""Health-check framework — MON-001, MON-004.

A check is a named, timeout-bounded coroutine that reports ``PASS``, ``DEGRADED``
or ``FAIL`` with detail. Checks marked ``critical`` gate trading: if any critical
check fails, the system reports ``TRADING DISABLED`` and every trading endpoint
refuses (MON-004).

Two properties matter more than the checks themselves:

* **A hanging check must not hang the system.** Each runs under ``wait_for``; a
  timeout is a failure, not a stall.
* **A check must report honestly.** A check that cannot determine its subject
  reports ``FAIL`` or ``SKIPPED`` with a reason — never ``PASS`` by default.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from app.core.clock import get_clock
from app.core.enums import HealthStatus
from app.core.logging import get_logger

logger = get_logger("monitoring.health")

__all__ = [
    "HealthCheck",
    "HealthResult",
    "HealthReport",
    "HealthRegistry",
    "get_health_registry",
    "reset_health_registry",
]


@dataclass(frozen=True)
class HealthResult:
    name: str
    status: HealthStatus
    critical: bool
    detail: str = ""
    duration_ms: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    checked_at: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return self.status in (HealthStatus.PASS, HealthStatus.DEGRADED, HealthStatus.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "critical": self.critical,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "context": self.context,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(frozen=True)
class HealthReport:
    results: tuple[HealthResult, ...]
    generated_at: datetime

    @property
    def failing_critical(self) -> tuple[HealthResult, ...]:
        return tuple(r for r in self.results if r.critical and r.status is HealthStatus.FAIL)

    @property
    def degraded(self) -> tuple[HealthResult, ...]:
        return tuple(r for r in self.results if r.status is HealthStatus.DEGRADED)

    @property
    def trading_allowed(self) -> bool:
        """MON-004: any failing critical check disables trading."""
        return not self.failing_critical

    @property
    def overall_status(self) -> HealthStatus:
        if self.failing_critical:
            return HealthStatus.FAIL
        if any(r.status is HealthStatus.FAIL for r in self.results) or self.degraded:
            return HealthStatus.DEGRADED
        return HealthStatus.PASS

    def blocking_reason(self) -> Optional[str]:
        failing = self.failing_critical
        if not failing:
            return None
        return "; ".join(f"{r.name}: {r.detail or r.status.value}" for r in failing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.overall_status.value,
            "trading_allowed": self.trading_allowed,
            "blocking_reason": self.blocking_reason(),
            "generated_at": self.generated_at.isoformat(),
            "checks": [r.to_dict() for r in self.results],
        }


class HealthCheck(ABC):
    """One named check."""

    #: Stable identifier, used in the API, UI and stored history.
    name: str = "unnamed"
    #: Critical checks gate trading.
    critical: bool = False
    timeout_seconds: float = 5.0

    @abstractmethod
    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        """Return ``(status, detail, context)``. Exceptions are caught by the runner."""

    async def execute(self) -> HealthResult:
        clock = get_clock()
        started = clock.monotonic()
        try:
            status, detail, context = await asyncio.wait_for(
                self.run(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            status, detail, context = (
                HealthStatus.FAIL,
                f"check timed out after {self.timeout_seconds}s",
                {},
            )
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            status, detail, context = (
                HealthStatus.FAIL,
                f"{type(exc).__name__}: health check failed",
                {"error_type": type(exc).__name__},
            )
        duration_ms = int((clock.monotonic() - started) * 1000)
        return HealthResult(
            name=self.name,
            status=status,
            critical=self.critical,
            detail=detail,
            duration_ms=duration_ms,
            context=context,
            checked_at=clock.now(),
        )


class HealthRegistry:
    """Holds the registered checks and the most recent report."""

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._last_report: Optional[HealthReport] = None

    def register(self, check: HealthCheck) -> None:
        if check.name in self._checks:
            logger.warning("Replacing existing health check", extra={"check": check.name})
        self._checks[check.name] = check

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    @property
    def last_report(self) -> Optional[HealthReport]:
        return self._last_report

    async def run_all(self, *, only: Optional[Sequence[str]] = None) -> HealthReport:
        """Run every registered check concurrently and cache the report."""
        checks = [c for name, c in self._checks.items() if only is None or name in only]
        results = await asyncio.gather(*(c.execute() for c in checks)) if checks else []
        report = HealthReport(results=tuple(results), generated_at=get_clock().now())
        self._last_report = report

        if report.failing_critical:
            logger.error(
                "TRADING DISABLED — critical health check failed",
                extra={
                    "failing": [r.name for r in report.failing_critical],
                    "reason": report.blocking_reason(),
                },
            )
        elif report.degraded:
            logger.warning(
                "System degraded",
                extra={"degraded": [r.name for r in report.degraded]},
            )
        return report


_registry = HealthRegistry()


def get_health_registry() -> HealthRegistry:
    return _registry


def reset_health_registry() -> HealthRegistry:
    """Test helper: start from an empty registry."""
    global _registry
    _registry = HealthRegistry()
    return _registry
