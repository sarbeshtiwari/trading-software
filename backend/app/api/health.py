"""Health endpoints — BE-002.

``/live`` answers "is this process running" — it must stay cheap and must not
touch dependencies, or a slow database would make an orchestrator kill a healthy
process.

``/ready`` answers "is this system fit to trade" and returns 503 with a
per-component breakdown when it is not.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.core.clock import get_clock
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.monitoring.startup import startup_check_coverage

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, Any]:
    return {"status": "alive", "at": get_clock().now().isoformat()}


@router.get("/ready", summary="Readiness and component health")
async def ready(response: Response) -> dict[str, Any]:
    report = await get_health_registry().run_all()
    gate = get_trading_gate()
    _present, missing = startup_check_coverage()
    trading_allowed = report.trading_allowed and gate.new_entries_allowed

    if not trading_allowed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        **report.to_dict(),
        "trading_allowed": trading_allowed,
        "status": report.overall_status.value if trading_allowed else "FAIL",
        "blocking_reason": report.blocking_reason() or gate.reason(),
        "gate": gate.state.to_dict(),
        "missing_checks": list(missing),
    }
