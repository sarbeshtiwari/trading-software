"""Independent JSON snapshots with exact Decimal values and secret redaction."""

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from app.core.clock import UTC
from app.core.logging import redact_data


def _encode(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite audit value")
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("naive audit timestamp")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError("unsupported audit snapshot value")


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_encode,
    )


def freeze_snapshot(value):
    return json.loads(canonical(redact_data(value)))
