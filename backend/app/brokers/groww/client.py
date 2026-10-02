"""Groww HTTP client — GRW-001, GRW-024, AUTH-002.

One place where every Groww request is built, throttled, retried, unwrapped and
logged. Everything above this layer deals in typed models and application errors.

Verified contract (2026-09-17):

* Base URL ``https://api.groww.in/v1``
* Headers ``Authorization: Bearer <token>``, ``Accept: application/json``,
  ``X-API-VERSION: 1.0``
* Envelope ``{"status": "SUCCESS"|"FAILURE", ...}``

Re-authentication is deliberately bounded: a 401 or ``GA005`` invalidates the
cached token and retries the original request **exactly once**. An unbounded
refresh loop would burn the 150-per-day token budget in seconds and lock the
account out of authentication for the rest of the day.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

import httpx

from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.envelope import unwrap
from app.brokers.groww.errors import GrowwAuthError, GrowwError, map_groww_error
from app.brokers.groww.ratelimit import RateCategory, RateLimiter, get_rate_limiter
from app.brokers.groww.retry import RetryPolicy, with_retry
from app.config import Settings, get_settings
from app.core.clock import Clock, get_clock
from app.core.errors import ConnectionFailedError, TimeoutError_
from app.core.logging import broker_logger, get_correlation_id, get_logger

logger = get_logger("brokers.groww.client")
wire_log = broker_logger()

__all__ = ["GrowwClient"]


class GrowwClient:
    """Async HTTP client for the Groww TradeAPI."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        authenticator: Optional[GrowwAuthenticator] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        rate_limiter: Optional[RateLimiter] = None,
        clock: Optional[Clock] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._clock = clock or get_clock()
        self._rate_limiter = rate_limiter or get_rate_limiter()
        self._authenticator = authenticator or GrowwAuthenticator(
            self._settings, clock=self._clock, rate_limiter=self._rate_limiter
        )
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=self._settings.groww_max_retries
        )
        self._transport = transport
        self._http: Optional[httpx.AsyncClient] = None
        #: Diagnostics for the metrics endpoint.
        self.request_count = 0
        self.error_count = 0

    # --- Lifecycle --------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._settings.groww_base_url.rstrip("/")

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._settings.groww_timeout_seconds),
                transport=self._transport,
                headers={
                    "Accept": "application/json",
                    "X-API-VERSION": self._settings.groww_api_version,
                    "User-Agent": f"{self._settings.app_name}/{self._settings.version}",
                },
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def authenticator(self) -> GrowwAuthenticator:
        return self._authenticator

    # --- Requests ---------------------------------------------------------

    async def get(
        self,
        path: str,
        *,
        category: RateCategory = RateCategory.NON_TRADING,
        params: Optional[Mapping[str, Any]] = None,
        allow_retry: bool = True,
    ) -> Any:
        return await self.request(
            "GET", path, category=category, params=params, allow_retry=allow_retry
        )

    async def post(
        self,
        path: str,
        *,
        category: RateCategory = RateCategory.NON_TRADING,
        json: Optional[Mapping[str, Any]] = None,
        allow_retry: bool = True,
    ) -> Any:
        return await self.request(
            "POST", path, category=category, json=json, allow_retry=allow_retry
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        category: RateCategory,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        allow_retry: bool = True,
        authenticated: bool = True,
    ) -> Any:
        """Perform one API call and return its unwrapped payload.

        ``allow_retry=False`` is mandatory for order-creating calls: a timeout
        there must be resolved by reconciliation, not by resending (EXEC-005).
        """

        async def _attempt() -> Any:
            return await self._send(
                method,
                path,
                category=category,
                params=params,
                json=json,
                authenticated=authenticated,
                allow_reauth=True,
            )

        return await with_retry(
            _attempt,
            policy=self._retry_policy,
            description=f"{method} {path}",
            allow_retry=allow_retry,
            clock=self._clock,
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        category: RateCategory,
        params: Optional[Mapping[str, Any]],
        json: Optional[Mapping[str, Any]],
        authenticated: bool,
        allow_reauth: bool,
    ) -> Any:
        await self._rate_limiter.acquire(category)

        headers: dict[str, str] = {}
        if authenticated:
            token = await self._authenticator.get_token()
            headers["Authorization"] = f"Bearer {token}"

        correlation = get_correlation_id()
        if correlation:
            headers["X-Request-Id"] = correlation

        client = self._ensure_http()
        started = self._clock.monotonic()
        self.request_count += 1

        try:
            response = await client.request(method, path, params=params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            self.error_count += 1
            self._log_wire(method, path, category, None, started, error=str(exc))
            raise TimeoutError_(
                f"{method} {path} timed out after {self._settings.groww_timeout_seconds}s",
                context={"path": path, "method": method},
            ) from exc
        except httpx.HTTPError as exc:
            self.error_count += 1
            self._log_wire(method, path, category, None, started, error=str(exc))
            raise ConnectionFailedError(
                f"{method} {path} failed to reach Groww: {exc}",
                context={"path": path, "method": method},
            ) from exc

        return await self._handle_response(
            response,
            method=method,
            path=path,
            category=category,
            params=params,
            json=json,
            started=started,
            authenticated=authenticated,
            allow_reauth=allow_reauth,
        )

    async def _handle_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        category: RateCategory,
        params: Optional[Mapping[str, Any]],
        json: Optional[Mapping[str, Any]],
        started: float,
        authenticated: bool,
        allow_reauth: bool,
    ) -> Any:
        try:
            body: Any = response.json()
        except ValueError:
            body = None

        self._log_wire(method, path, category, response.status_code, started)

        if body is None:
            self.error_count += 1
            raise map_groww_error(
                None,
                f"Groww returned a non-JSON body (HTTP {response.status_code})",
                http_status=response.status_code,
                context={
                    "path": path,
                    "response_class": (
                        "EMPTY"
                        if not response.content
                        else "HTML"
                        if "text/html" in response.headers.get("content-type", "").lower()
                        else "NON_JSON"
                    ),
                },
            )

        try:
            return unwrap(body, http_status=response.status_code, path=path)
        except GrowwAuthError as error:
            error.context["response_class"] = "JSON_BROKER_FAILURE"
            self.error_count += 1
            if not (authenticated and allow_reauth):
                raise
            # Exactly one re-authentication, then one replay of the original call.
            logger.warning(
                "Groww rejected the token; re-authenticating once",
                extra={"path": path, "method": method},
            )
            self._authenticator.invalidate()
            await self._authenticator.get_token(force_refresh=True)
            return await self._send(
                method,
                path,
                category=category,
                params=params,
                json=json,
                authenticated=authenticated,
                allow_reauth=False,
            )
        except GrowwError as error:
            error.context["response_class"] = "JSON_BROKER_FAILURE"
            self.error_count += 1
            raise

    # --- Logging (GRW-024, LOG-004) ---------------------------------------

    def _log_wire(
        self,
        method: str,
        path: str,
        category: RateCategory,
        status_code: Optional[int],
        started: float,
        *,
        error: Optional[str] = None,
    ) -> None:
        """Log the call. The redaction filter removes any token before output."""
        wire_log.info(
            "groww %s %s",
            method,
            path,
            extra={
                "broker": "groww",
                "http_method": method,
                "path": path,
                "status_code": status_code,
                "rate_category": category.value,
                "latency_ms": int((self._clock.monotonic() - started) * 1000),
                "error": error,
            },
        )

    # --- Probe ------------------------------------------------------------

    async def ping(self) -> bool:
        """Cheap authenticated call proving credentials and connectivity work."""
        try:
            await self.get(Endpoints.MARGIN.resolve(), category=RateCategory.NON_TRADING)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.warning("Groww ping failed", extra={"error": str(exc)})
            return False
