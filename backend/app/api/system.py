"""System status — BE-003.

Every field here reflects real internal state. Nothing is a constant: the mode is
the resolved process mode, the gate state is the live gate, and the health
summary is the most recent report rather than an assumption.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter

from app.config import get_settings
from app.core.clock import get_clock
from app.core.lifecycle import get_lifecycle
from app.modes import current_mode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry

router = APIRouter(prefix="/system", tags=["system"])

_STARTED_MONOTONIC: Optional[float] = None


def mark_started() -> None:
    """Record process start for uptime reporting."""
    global _STARTED_MONOTONIC
    _STARTED_MONOTONIC = get_clock().monotonic()


@router.get("/status", summary="Mode, trading state, version and uptime")
async def status() -> dict[str, Any]:
    settings = get_settings()
    gate = get_trading_gate()
    last_report = get_health_registry().last_report
    clock = get_clock()

    uptime = None
    if _STARTED_MONOTONIC is not None:
        uptime = round(clock.monotonic() - _STARTED_MONOTONIC, 1)

    return {
        "app_name": settings.app_name,
        "version": settings.version,
        "git_commit": settings.git_commit,
        "environment": settings.environment.value,
        "trading_mode": current_mode().value,
        "broker_provider": settings.broker_provider.value,
        "llm_provider": settings.effective_llm_provider.value,
        "gate": gate.state.to_dict(),
        "health": {
            "status": last_report.overall_status.value if last_report else "UNKNOWN",
            "generated_at": last_report.generated_at.isoformat() if last_report else None,
        },
        "shutting_down": get_lifecycle().shutting_down,
        "uptime_seconds": uptime,
        "server_time_ist": clock.now().isoformat(),
    }


@router.get("/config", summary="Effective configuration with secrets redacted")
async def config() -> dict[str, Any]:
    return get_settings().redacted_summary()
