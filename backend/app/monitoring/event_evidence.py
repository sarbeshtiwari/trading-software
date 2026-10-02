"""Payload-free retained-event summaries shared by monitoring and audit publication."""

import hashlib
import re

from app.analysis.equity import EvidenceModel
from app.core.events import EventType


class RetainedFailure(EvidenceModel):
    receipt_id: str
    event_type: str
    entry_id: str | None
    handler_reference: str
    category: str


def _text(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")


def summarize_failure(identifier, fields):
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
