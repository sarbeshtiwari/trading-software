"""Read-only, payload-free inspection of retained Redis event failures."""

import asyncio
import logging

from fastapi import APIRouter, Request
from redis.asyncio import Redis

from app.analysis.equity import EvidenceModel
from app.config import get_settings
from app.monitoring.event_evidence import RetainedFailure, summarize_failure

router = APIRouter(prefix="/events", tags=["monitoring"])
STREAM = "ats:events:dead-letter"
logger = logging.getLogger(__name__)


class EventFailureView(EvidenceModel):
    status: str
    publication_status: str = "NOT_RUNNING"
    retained_count: int | None = None
    failures: tuple[RetainedFailure, ...] = ()
    has_more: bool = False
    scope: str = (
        "Retained event failures only; runtime event integration and external alert delivery "
        "are not certified. Raw payloads and exception messages are withheld."
    )


async def _inspect(client):
    async with client.pipeline(transaction=True) as pipeline:
        pipeline.xlen(STREAM)
        pipeline.xrevrange(STREAM, count=50)
        count, records = await pipeline.execute()
    return EventFailureView(
        status="RETAINED_FAILURES" if count else "NO_RETAINED_FAILURES",
        retained_count=count,
        failures=tuple(summarize_failure(identifier, fields) for identifier, fields in records),
        has_more=count > len(records),
    )


@router.get("/failures", response_model=EventFailureView)
async def failures(request: Request):
    client = Redis.from_url(get_settings().redis_url, socket_timeout=2, socket_connect_timeout=5)
    try:
        result = await asyncio.wait_for(_inspect(client), timeout=6)
        return result.model_copy(
            update={"publication_status": request.app.state.event_failure_monitor.status}
        )
    except Exception as error:
        logger.warning(
            "Event failure inspection unavailable", extra={"error_type": type(error).__name__}
        )
        return EventFailureView(
            status="EVENT_MONITOR_UNAVAILABLE",
            publication_status=request.app.state.event_failure_monitor.status,
        )
    finally:
        await client.aclose()
