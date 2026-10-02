"""Explicit owner-run authentication/LTP check; no order or account mutation API."""

import asyncio
import contextlib
import io
import json
import logging
from decimal import Decimal, InvalidOperation

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.errors import GROWW_ERROR_CODES
from app.config import get_settings
from app.core.clock import get_clock
from app.core.logging import register_secret, register_secrets


async def verify(settings, *, client=None):
    report = {
        "checked_at": get_clock().utcnow().isoformat(),
        "scope": "AUTH_AND_READ_ONLY_INDEX_LTP",
        "authentication": "NOT_CHECKED",
        "market_data": "NOT_CHECKED",
        "order_requests": 0,
        "live_execution": "UNVERIFIED",
        "freshness_for_execution": "NOT_VERIFIED",
        "endpoint": "https://api.groww.in/v1/live-data/ltp",
        "method": "GET",
        "api_version": settings.groww_api_version,
        "configuration_source": "APPLICATION_SETTINGS",
        "subscription_state": "UNOBSERVED",
    }
    if settings.groww_base_url.rstrip("/") != "https://api.groww.in/v1":
        return report | {"error_type": "NONSTANDARD_HOST_REFUSED"}
    register_secrets(settings.secret_values)
    owned = client is None
    client = client or GrowwClient(settings=settings.model_copy(update={"groww_max_retries": 1}))
    try:
        token = await client.authenticator.get_token()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("access token unavailable")
        register_secret(token)
        report["authentication"] = "VERIFIED"
        payload = await client.get(
            "/live-data/ltp",
            params={"segment": "CASH", "exchange_symbols": "NSE_NIFTY"},
            allow_retry=False,
        )
        try:
            value = Decimal(str(payload.get("NSE_NIFTY"))) if isinstance(payload, dict) else None
        except (InvalidOperation, ValueError):
            value = None
        report["market_data"] = (
            "VERIFIED" if value is not None and value.is_finite() and value > 0 else "UNAVAILABLE"
        )
    except Exception as error:
        report["error_type"] = type(error).__name__
        field = "market_data" if report["authentication"] == "VERIFIED" else "authentication"
        report[field] = "FAILED"
        code = getattr(error, "broker_code", None)
        if code in GROWW_ERROR_CODES:
            report["broker_code"] = code
        status = getattr(error, "http_status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            report["http_status"] = status
        context = getattr(error, "context", {})
        classification = context.get("response_class") if isinstance(context, dict) else None
        if classification in {"EMPTY", "HTML", "NON_JSON", "JSON_BROKER_FAILURE"}:
            report["response_class"] = classification
    finally:
        if owned:
            await client.aclose()
    return report


def main():
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            report = asyncio.run(verify(get_settings()))
        except Exception as error:
            report = {
                "error_type": type(error).__name__,
                "order_requests": 0,
                "live_execution": "UNVERIFIED",
            }
    print(json.dumps(report))
    return 0 if report.get("market_data") == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
