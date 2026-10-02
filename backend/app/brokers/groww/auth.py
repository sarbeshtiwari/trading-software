"""Groww authentication — AUTH-001…AUTH-003, AUTH-005.

Two documented flows:

* **Flow A — API key + secret.** Produces a *daily* access token, requires daily
  approval, and the token endpoint is capped at **150 requests per 24 hours**.
* **Flow B — TOTP.** A TOTP token plus its shared secret; ``pyotp`` generates the
  six-digit code at call time. The resulting access token does not expire, which
  is why this flow is preferred and is selected automatically when both are
  configured.

Token exchange is pluggable:

* :class:`SdkTokenExchange` calls ``growwapi.GrowwAPI.get_access_token`` — the
  exact call the vendor documents. Preferred, because it cannot drift from the
  documented request shape.
* :class:`RestTokenExchange` posts to ``/token/api/access`` directly. The body
  field names for this endpoint are **not published** in the REST documentation,
  so this path is marked unverified and logs a warning when used. It exists so the
  system has no hard dependency on the SDK; the SDK path is the default.

Nothing here ever logs a credential: secrets are registered with the log redactor
at startup, and this module only ever logs which flow was used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Protocol

from app.config import Settings, get_settings
from app.core.clock import IST, Clock, get_clock
from app.core.errors import ConfigurationError, RateLimitError
from app.core.logging import get_logger
from app.brokers.groww.errors import GrowwAuthError
from app.brokers.groww.ratelimit import RateCategory, RateLimiter, get_rate_limiter

logger = get_logger("brokers.groww.auth")

__all__ = [
    "AccessToken",
    "TokenExchange",
    "SdkTokenExchange",
    "RestTokenExchange",
    "GrowwAuthenticator",
    "generate_totp",
]

TOKEN_PATH = "/token/api/access"


def generate_totp(secret: str) -> str:
    """Generate the current six-digit TOTP code."""
    try:
        import pyotp  # noqa: PLC0415 - optional at import time, required for flow B
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ConfigurationError(
            "pyotp is required for the Groww TOTP auth flow (pip install pyotp)"
        ) from exc
    return pyotp.TOTP(secret).now()


@dataclass(frozen=True)
class AccessToken:
    value: str
    #: ``None`` means no expiry (flow B).
    expires_at: Optional[datetime]
    flow: str
    issued_at: datetime

    def is_expired(self, now: datetime, *, skew_seconds: int = 60) -> bool:
        if self.expires_at is None:
            return False
        return now >= self.expires_at - timedelta(seconds=skew_seconds)


class TokenExchange(Protocol):
    """Turns credentials into an access token."""

    async def exchange(self, settings: Settings) -> tuple[str, Optional[datetime], str]:
        """Return ``(token, expires_at, flow)``."""


class SdkTokenExchange:
    """Uses the vendor SDK, which is the documented way to mint a token."""

    name = "sdk"

    async def exchange(self, settings: Settings) -> tuple[str, Optional[datetime], str]:
        try:
            from growwapi import GrowwAPI  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise ConfigurationError(
                "growwapi is not installed. Install it (`pip install growwapi`) or set "
                "GROWW_TOKEN_EXCHANGE=rest to use the direct REST call.",
            ) from exc

        flow = settings.groww_auth_flow
        if flow == "totp":
            api_key = settings.groww_totp_token.get_secret_value()  # type: ignore[union-attr]
            secret = settings.groww_totp_secret.get_secret_value()  # type: ignore[union-attr]
            code = generate_totp(secret)
            token = await asyncio.to_thread(GrowwAPI.get_access_token, api_key=api_key, totp=code)
            # Flow B tokens are documented as not expiring.
            return str(token), None, "totp"

        if flow == "api_key":
            api_key = settings.groww_api_key.get_secret_value()  # type: ignore[union-attr]
            secret = settings.groww_api_secret.get_secret_value()  # type: ignore[union-attr]
            token = await asyncio.to_thread(
                GrowwAPI.get_access_token, api_key=api_key, secret=secret
            )
            return str(token), _end_of_ist_day(get_clock().now()), "api_key"

        raise ConfigurationError(
            "No Groww credentials configured. Set GROWW_TOTP_TOKEN + GROWW_TOTP_SECRET "
            "(preferred) or GROWW_API_KEY + GROWW_API_SECRET."
        )


class RestTokenExchange:
    """Direct POST to the token endpoint.

    .. warning::
       **Unverified request shape.** Groww documents the endpoint path and the
       response envelope, but not the field names of this request body. The body
       below is a best-effort reading and must be confirmed against a real
       account before LIVE use. Recorded in ``docs/LIMITATIONS.md``.
    """

    name = "rest"

    def __init__(self, post: Any) -> None:
        #: ``async (path, json) -> payload``; injected so this stays testable.
        self._post = post

    async def exchange(self, settings: Settings) -> tuple[str, Optional[datetime], str]:
        flow = settings.groww_auth_flow
        if flow is None:
            raise ConfigurationError("No Groww credentials configured.")

        logger.warning(
            "Using the direct REST token exchange; its request body is unverified "
            "against a live account. Prefer the SDK exchange.",
            extra={"flow": flow},
        )

        if flow == "totp":
            body = {
                "key_type": "totp",
                "totp": generate_totp(
                    settings.groww_totp_secret.get_secret_value()  # type: ignore[union-attr]
                ),
            }
            api_key = settings.groww_totp_token.get_secret_value()  # type: ignore[union-attr]
            expires_at = None
        else:
            body = {
                "key_type": "approval",
                "checksum": settings.groww_api_secret.get_secret_value(),  # type: ignore[union-attr]
            }
            api_key = settings.groww_api_key.get_secret_value()  # type: ignore[union-attr]
            expires_at = _end_of_ist_day(get_clock().now())

        payload = await self._post(TOKEN_PATH, body, api_key)
        token = _extract_token(payload)
        return token, expires_at, flow


def _extract_token(payload: Any) -> str:
    """Pull the token out of the payload without guessing silently."""
    if isinstance(payload, str) and payload:
        return payload
    if isinstance(payload, dict):
        for key in ("token", "access_token", "accessToken"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    raise GrowwAuthError(
        "Token endpoint returned a payload with no recognisable access token",
        context={"payload_keys": sorted(payload) if isinstance(payload, dict) else None},
    )


def _end_of_ist_day(now: datetime) -> datetime:
    """Flow A tokens are daily; treat them as valid until midnight IST."""
    return datetime.combine(now.date(), time(23, 59, 59), tzinfo=IST)


class GrowwAuthenticator:
    """Caches the access token and guards the daily token-endpoint budget."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        exchange: Optional[TokenExchange] = None,
        clock: Optional[Clock] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._exchange = exchange or SdkTokenExchange()
        self._clock = clock or get_clock()
        self._rate_limiter = rate_limiter or get_rate_limiter()
        self._token: Optional[AccessToken] = None
        self._lock = asyncio.Lock()
        self._requests_today = 0
        self._counter_date: Optional[date] = None

    # --- Budget (AUTH-003) ------------------------------------------------

    @property
    def token_requests_today(self) -> int:
        self._roll_counter()
        return self._requests_today

    def _roll_counter(self) -> None:
        today = self._clock.today()
        if self._counter_date != today:
            self._counter_date = today
            self._requests_today = 0

    def _check_budget(self) -> None:
        self._roll_counter()
        budget = self._settings.groww_daily_token_budget
        if self._requests_today >= budget:
            raise RateLimitError(
                f"Daily Groww token-endpoint budget of {budget} requests is exhausted "
                f"({self._requests_today} used). Further attempts would be rejected by the "
                f"broker for the rest of the day; fix the cause of the re-authentication "
                f"loop rather than retrying.",
                context={"used": self._requests_today, "budget": budget},
            )

    # --- Token ------------------------------------------------------------

    @property
    def cached_token(self) -> Optional[AccessToken]:
        return self._token

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, minting one only when necessary."""
        async with self._lock:
            now = self._clock.now()
            if (
                not force_refresh
                and self._token is not None
                and not self._token.is_expired(now)
            ):
                return self._token.value

            self._check_budget()
            await self._rate_limiter.acquire(RateCategory.AUTH)

            started = self._clock.monotonic()
            value, expires_at, flow = await self._exchange.exchange(self._settings)
            self._requests_today += 1

            self._token = AccessToken(
                value=value, expires_at=expires_at, flow=flow, issued_at=now
            )
            logger.info(
                "Obtained Groww access token",
                extra={
                    "auth_flow": flow,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "latency_ms": int((self._clock.monotonic() - started) * 1000),
                    "token_requests_today": self._requests_today,
                },
            )
            return self._token.value

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-authenticates once."""
        if self._token is not None:
            logger.info("Invalidating cached Groww access token")
        self._token = None
