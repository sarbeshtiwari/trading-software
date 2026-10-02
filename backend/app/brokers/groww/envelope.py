"""Groww response envelope — GRW-002.

Every Groww response is wrapped:

```json
{"status": "SUCCESS", "payload": {...}}
{"status": "FAILURE", "error": {"code": "GA001", "message": "Bad request"}}
```

Unwrapping is a separate, tested function because the failure mode it guards
against is the worst kind: a response that does not match either shape must raise
rather than silently produce ``None``. A ``None`` payload flowing into order
handling is how a system decides it has no positions when it does.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.errors import InvalidResponseError
from app.brokers.groww.errors import map_groww_error

__all__ = ["unwrap", "is_success", "SUCCESS", "FAILURE"]

SUCCESS = "SUCCESS"
FAILURE = "FAILURE"


def is_success(body: Any) -> bool:
    return isinstance(body, dict) and str(body.get("status", "")).upper() == SUCCESS


def unwrap(
    body: Any,
    *,
    http_status: Optional[int] = None,
    path: Optional[str] = None,
) -> Any:
    """Return the payload, or raise the mapped error.

    Raises
    ------
    InvalidResponseError
        When the body is not a dict, carries no recognisable ``status``, or
        reports success without a payload key.
    GrowwError
        When the body reports ``FAILURE``.
    """
    context = {"path": path} if path else None

    if not isinstance(body, dict):
        raise InvalidResponseError(
            f"Groww response was {type(body).__name__}, expected a JSON object",
            context={"path": path, "http_status": http_status},
        )

    status = str(body.get("status", "")).upper()

    if status == SUCCESS:
        if "payload" not in body:
            raise InvalidResponseError(
                "Groww response reported SUCCESS but carried no payload",
                context={"path": path, "keys": sorted(body)},
            )
        return body["payload"]

    if status == FAILURE:
        error = body.get("error") or {}
        if not isinstance(error, dict):
            error = {"message": str(error)}
        raise map_groww_error(
            error.get("code"),
            error.get("message"),
            http_status=http_status,
            context=context,
        )

    # Neither shape. This is where a changed API contract surfaces, so it must be
    # loud and must name what was actually received.
    raise InvalidResponseError(
        f"Groww response had no recognisable status (got {body.get('status')!r})",
        context={"path": path, "http_status": http_status, "keys": sorted(body)},
    )
