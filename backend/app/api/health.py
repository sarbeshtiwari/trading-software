"""Health endpoints — BE-002.

``/live`` answers "is this process running" — it must stay cheap and must not
touch dependencies, or a slow database would make an orchestrator kill a healthy
process.

``/ready`` answers "is this system fit to trade" and returns 503 with a
per-component breakdown when it is not.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime

from app.config import get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.monitoring.history import HealthHistory, HistoryTooLarge, read_history
from app.monitoring.startup import startup_check_coverage

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/history", response_model=HealthHistory)
async def history(
    from_at: AwareDatetime | None = None,
    to_at: AwareDatetime | None = None,
    offset: int = Query(default=0, ge=0, le=10000),
    limit: int = Query(default=50, ge=1, le=100),
):
    now = get_clock().utcnow()
    end = to_at or now
    start = from_at or end - timedelta(days=1)
    if not start <= end <= now or end - start > timedelta(days=31):
        raise HTTPException(422, "Use an ordered, non-future interval of at most 31 days")

    async def load():
        async with db_session.session_scope() as session:
            return await read_history(
                session, get_settings().trading_mode, start, end, now, offset=offset, limit=limit
            )

    try:
        return await asyncio.wait_for(load(), timeout=8)
    except HistoryTooLarge:
        raise HTTPException(413, "Health history exceeds verification capacity") from None
    except ValueError:
        raise HTTPException(409, "Health history evidence is invalid or unavailable") from None
    except Exception:
        raise HTTPException(503, "Health history storage is unavailable") from None


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
