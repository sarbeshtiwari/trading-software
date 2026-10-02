"""Harness for the Groww adapter tests.

Requests are served by an ``httpx`` mock transport so the **real client code**
runs end to end — headers, rate limiting, retries, envelope handling, error
mapping and parsing — against the contract fixtures. What is not exercised is the
live API itself, which is exactly the boundary recorded in
``docs/LIMITATIONS.md``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.client import GrowwClient
from app.brokers.groww.ratelimit import RateLimiter
from app.config import Settings
from app.core.clock import FakeClock, SystemClock

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "groww"

__all__ = [
    "FIXTURE_ROOT",
    "load_fixture",
    "StubTokenExchange",
    "MockGroww",
]


def load_fixture(name: str) -> dict[str, Any]:
    """Load a contract fixture by file name."""
    path = FIXTURE_ROOT / name
    if not path.exists():  # pragma: no cover - a typo in a test
        raise FileNotFoundError(f"no such fixture: {path}")
    return json.loads(io.open(path, encoding="utf-8").read())


class StubTokenExchange:
    """Returns a token without touching the SDK or the network."""

    def __init__(self, token: str = "test-access-token", expires_at: Any = None) -> None:
        self.token = token
        self.expires_at = expires_at
        self.calls = 0

    async def exchange(self, settings: Settings):  # type: ignore[no-untyped-def]
        self.calls += 1
        return f"{self.token}-{self.calls}", self.expires_at, "totp"


Handler = Callable[[httpx.Request], httpx.Response]


class MockGroww:
    """Routes requests by path and records everything that was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[str, Handler] = {}
        self._default: Optional[Handler] = None

    # --- Route registration ------------------------------------------------

    def on(self, path: str, handler: Handler) -> "MockGroww":
        self._routes[path] = handler
        return self

    def json(self, path: str, body: dict[str, Any], status: int = 200) -> "MockGroww":
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=body)

        return self.on(path, handler)

    def fixture(self, path: str, name: str, status: int = 200) -> "MockGroww":
        return self.json(path, load_fixture(name), status=status)

    def sequence(self, path: str, responses: list[httpx.Response]) -> "MockGroww":
        """Serve the given responses in order, repeating the last one."""
        state = {"index": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            index = min(state["index"], len(responses) - 1)
            state["index"] += 1
            return responses[index]

        return self.on(path, handler)

    def timeout(self, path: str, times: int = 1) -> "MockGroww":
        state = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["count"] += 1
            if state["count"] <= times:
                raise httpx.ReadTimeout("simulated timeout", request=request)
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {}})

        return self.on(path, handler)

    def default(self, handler: Handler) -> "MockGroww":
        self._default = handler
        return self

    # --- Transport ---------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        def dispatch(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            handler = self._routes.get(request.url.path)
            if handler is not None:
                return handler(request)
            if self._default is not None:
                return self._default(request)
            return httpx.Response(
                404,
                json={
                    "status": "FAILURE",
                    "error": {"code": "GA004", "message": f"no route for {request.url.path}"},
                },
            )

        return httpx.MockTransport(dispatch)

    # --- Assertions --------------------------------------------------------

    def count(self, path: str) -> int:
        return sum(1 for request in self.requests if request.url.path == path)

    def last(self, path: Optional[str] = None) -> httpx.Request:
        for request in reversed(self.requests):
            if path is None or request.url.path == path:
                return request
        raise AssertionError(f"no request recorded for {path}")

    def body(self, path: Optional[str] = None) -> dict[str, Any]:
        return json.loads(self.last(path).content.decode())


@pytest.fixture
def groww_settings(settings_env) -> Settings:
    return settings_env(
        STARTING_CAPITAL="500000",
        GROWW_TOTP_TOKEN="totp-token",
        GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP",
        GROWW_BASE_URL="https://api.groww.in/v1",
        GROWW_MAX_RETRIES="3",
    )


@pytest.fixture
def mock_groww() -> MockGroww:
    return MockGroww()


@pytest.fixture
def token_exchange() -> StubTokenExchange:
    return StubTokenExchange()


@pytest.fixture
def groww_client(groww_settings, mock_groww, token_exchange, fake_clock: FakeClock):
    """A real client wired to the mock transport.

    The rate limiter deliberately keeps the **real** monotonic clock while the
    rest of the client uses the fake one. The limiter waits on real time, so a
    frozen clock would make a legitimate multi-call test (14 history windows
    against a 10/second limit) block forever instead of simply pacing. Tests that
    assert limiter behaviour build their own limiter on the fake clock.
    """
    # SystemClock explicitly: the fake clock is installed globally by the
    # fixture, and get_clock() would pick it up.
    limiter = RateLimiter(clock=SystemClock())
    authenticator = GrowwAuthenticator(
        groww_settings, exchange=token_exchange, clock=fake_clock, rate_limiter=limiter
    )
    return GrowwClient(
        groww_settings,
        authenticator=authenticator,
        transport=mock_groww.transport(),
        rate_limiter=limiter,
        clock=fake_clock,
    )
