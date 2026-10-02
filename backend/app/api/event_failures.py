"""Read-only, payload-free inspection of retained Redis event failures."""

import asyncio
import hashlib
import logging
import re

from fastapi import APIRouter
from redis.asyncio import Redis

from app.analysis.equity import EvidenceModel
from app.config import get_settings
from app.core.events import EventType

router = APIRouter(prefix="/events", tags=["monitoring"])
STREAM = "ats:events:dead-letter"
logger = logging.getLogger(__name__)


class RetainedFailure(EvidenceModel):
    receipt_id: str
    event_type: str
    entry_id: str | None
    handler_reference: str
    category: str


class EventFailureView(EvidenceModel):
    status: str
    retained_count: int | None = None
    failures: tuple[RetainedFailure, ...] = ()
    has_more: bool = False
    scope: str = (
        "Retained event failures only; runtime event integration and external alert delivery "
        "are not certified. Raw payloads and exception messages are withheld."
    )


def _text(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")


def _summary(identifier, fields):
    values = {_text(key): _text(value) for key, value in fields.items() if _text(key) != "data"}
    event_type = values.get("stream", "").rsplit(":", 1)[-1]
    if event_type not in {item.value for item in EventType}:
        event_type = "UNKNOWN"
    entry = values.get("entry_id", "")
    reason = values.get("reason", "")
    category = (
        reason if reason in {"INVALID_ENVELOPE", "RETRY_BUDGET_EXHAUSTED"} else "HANDLER_FAILURE"
    )
    return RetainedFailure(
        receipt_id=_text(identifier),
        event_type=event_type,
        entry_id=entry if re.fullmatch(r"[0-9]+-[0-9]+", entry) else None,
        handler_reference=hashlib.sha256(values.get("handler", "").encode()).hexdigest()[:16],
        category=category,
    )


async def _inspect(client):
    async with client.pipeline(transaction=True) as pipeline:
        pipeline.xlen(STREAM)
        pipeline.xrevrange(STREAM, count=50)
        count, records = await pipeline.execute()
    return EventFailureView(
        status="RETAINED_FAILURES" if count else "NO_RETAINED_FAILURES",
        retained_count=count,
        failures=tuple(_summary(identifier, fields) for identifier, fields in records),
        has_more=count > len(records),
    )


@router.get("/failures", response_model=EventFailureView)
async def failures():
    client = Redis.from_url(get_settings().redis_url, socket_timeout=2, socket_connect_timeout=5)
    try:
        return await asyncio.wait_for(_inspect(client), timeout=6)
    except Exception as error:
        logger.warning(
            "Event failure inspection unavailable", extra={"error_type": type(error).__name__}
        )
        return EventFailureView(status="EVENT_MONITOR_UNAVAILABLE")
    finally:
        await client.aclose()
