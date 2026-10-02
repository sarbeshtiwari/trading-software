"""Groww client: headers, envelope, errors, rate limiting, retries, auth.

Covers GRW-001…GRW-006, GRW-024, AUTH-001…AUTH-003, ERR-002.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Confidence, Endpoints, inferred_endpoints
from app.brokers.groww.envelope import unwrap
from app.brokers.groww.errors import (
    DuplicateOrderReferenceError,
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwNotFoundError,
    GrowwPermanentError,
    GrowwTransientError,
    map_groww_error,
)
from app.brokers.groww.ratelimit import (
    DOCUMENTED_LIMITS,
    RateCategory,
    RateLimiter,
    SlidingWindowCounter,
)
from app.brokers.groww.retry import RetryPolicy, with_retry
from app.core.clock import IST, FakeClock
from app.core.errors import (
    ConnectionFailedError,
    InvalidResponseError,
    RateLimitError,
    TimeoutError_,
    TransientError,
)

from tests.integration.groww.conftest import MockGroww, StubTokenExchange, load_fixture

pytestmark = pytest.mark.integration

PATH = "/v1/margins/detail/user"


# --- GRW-001 --------------------------------------------------------------


async def test_groww_client_headers(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.fixture(PATH, "margin.json")
    await groww_client.get(Endpoints.MARGIN.resolve())

    request = mock_groww.last()
    assert request.headers["Authorization"] == "Bearer test-access-token-1"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["X-API-VERSION"] == "1.0"
    assert str(request.url).startswith("https://api.groww.in/v1")


async def test_base_url_is_configurable(settings_env, mock_groww, token_exchange, fake_clock):
    settings = settings_env(
        STARTING_CAPITAL="500000",
        GROWW_TOTP_TOKEN="t",
        GROWW_TOTP_SECRET="s",
        GROWW_BASE_URL="https://sandbox.example/v9",
    )
    limiter = RateLimiter(clock=fake_clock)
    client = GrowwClient(
        settings,
        authenticator=GrowwAuthenticator(
            settings, exchange=token_exchange, clock=fake_clock, rate_limiter=limiter
        ),
        transport=mock_groww.transport(),
        rate_limiter=limiter,
        clock=fake_clock,
    )
    mock_groww.json("/v9/margins/detail/user", load_fixture("margin.json"))
    await client.get(Endpoints.MARGIN.resolve())
    assert str(mock_groww.last().url).startswith("https://sandbox.example/v9")


# --- GRW-002 --------------------------------------------------------------


def test_groww_envelope() -> None:
    assert unwrap({"status": "SUCCESS", "payload": {"a": 1}}) == {"a": 1}
    assert unwrap({"status": "SUCCESS", "payload": []}) == []

    with pytest.raises(GrowwBadRequestError):
        unwrap(load_fixture("bad_request.json"))

    # A payload-less success is a contract break, not an empty result.
    with pytest.raises(InvalidResponseError):
        unwrap({"status": "SUCCESS"})
    with pytest.raises(InvalidResponseError):
        unwrap({"unexpected": "shape"})
    with pytest.raises(InvalidResponseError):
        unwrap("not an object")


# --- GRW-003 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        ("GA000", GrowwTransientError, True),
        ("GA001", GrowwBadRequestError, False),
        ("GA003", GrowwTransientError, True),
        ("GA004", GrowwNotFoundError, False),
        ("GA005", GrowwAuthError, False),
        ("GA006", GrowwPermanentError, False),
        ("GA007", DuplicateOrderReferenceError, False),
    ],
)
def test_groww_error_mapping(code: str, expected: type, retryable: bool) -> None:
    error = map_groww_error(code, "message")
    assert isinstance(error, expected)
    assert error.retryable is retryable
    assert error.broker_code == code
    assert code in str(error)


def test_unknown_codes_are_permanent() -> None:
    """An unrecognised code must not be retried; novelty is not transience."""
    assert map_groww_error("GA999", "who knows").retryable is False
    assert isinstance(map_groww_error(None, "boom", http_status=500), GrowwTransientError)
    assert isinstance(map_groww_error(None, "nope", http_status=401), GrowwAuthError)
    assert isinstance(map_groww_error(None, "gone", http_status=404), GrowwNotFoundError)
    assert map_groww_error(None, "slow down", http_status=429).retryable is True


# --- GRW-004 --------------------------------------------------------------


def test_documented_limits_match_the_specification() -> None:
    assert DOCUMENTED_LIMITS[RateCategory.AUTH] == (5, 30)
    assert DOCUMENTED_LIMITS[RateCategory.ORDERS] == (10, 250)
    assert DOCUMENTED_LIMITS[RateCategory.LIVE_DATA] == (10, 300)
    assert DOCUMENTED_LIMITS[RateCategory.NON_TRADING] == (20, 500)


def test_groww_ratelimit_buckets(fake_clock: FakeClock) -> None:
    limiter = RateLimiter(clock=fake_clock)

    # Ten order calls fit in one second; the eleventh does not.
    for _ in range(10):
        assert limiter.try_acquire(RateCategory.ORDERS)
    assert not limiter.try_acquire(RateCategory.ORDERS)
    assert limiter.delay_for(RateCategory.ORDERS) > 0

    fake_clock.advance_seconds(1)
    assert limiter.try_acquire(RateCategory.ORDERS)

    # Categories have independent budgets.
    assert limiter.try_acquire(RateCategory.LIVE_DATA)


def test_minute_window_binds_even_when_the_second_window_allows(fake_clock: FakeClock) -> None:
    """The published per-minute cap must hold across a full minute of traffic.

    A token bucket would allow capacity-plus-refill here (roughly 400 calls) and
    quietly breach a 250/minute limit. The sliding window does not.
    """
    limiter = RateLimiter(clock=fake_clock)
    taken = 0
    for _ in range(40):
        for _ in range(10):
            if limiter.try_acquire(RateCategory.ORDERS):
                taken += 1
        fake_clock.advance_seconds(1)

    assert taken == 250
    assert limiter.usage(RateCategory.ORDERS)[1] == 250

    # Once the oldest grants age out of the window, capacity returns.
    fake_clock.advance_seconds(60)
    assert limiter.try_acquire(RateCategory.ORDERS)


def test_sliding_window_counts_only_the_trailing_window(fake_clock: FakeClock) -> None:
    window = SlidingWindowCounter(limit=10, window_seconds=1.0, clock=fake_clock)
    for _ in range(10):
        assert window.try_take()
    assert not window.try_take()
    assert window.used() == 10
    assert window.seconds_until_free() == pytest.approx(1.0, abs=0.01)

    fake_clock.advance_seconds(0.5)
    assert not window.try_take()
    assert window.seconds_until_free() == pytest.approx(0.5, abs=0.01)

    fake_clock.advance_seconds(0.51)
    assert window.used() == 0
    assert window.try_take()


async def test_acquire_waits_rather_than_failing(fake_clock: FakeClock) -> None:
    limiter = RateLimiter({RateCategory.ORDERS: (1, 60)}, clock=fake_clock)
    await limiter.acquire(RateCategory.ORDERS)

    async def advance() -> None:
        # Let the limiter start waiting, then move time forward for it.
        for _ in range(20):
            await asyncio.sleep(0)
            fake_clock.advance_seconds(0.5)

    await asyncio.gather(limiter.acquire(RateCategory.ORDERS), advance())
    assert limiter.waits[RateCategory.ORDERS] >= 1


# --- GRW-005 --------------------------------------------------------------


async def test_groww_retry_policy(groww_client, mock_groww: MockGroww) -> None:
    """Transient failures retry; the call ultimately succeeds."""
    mock_groww.sequence(
        PATH,
        [
            httpx.Response(200, json=load_fixture("internal_error.json")),
            httpx.Response(200, json=load_fixture("margin.json")),
        ],
    )
    payload = await groww_client.get(Endpoints.MARGIN.resolve())
    assert payload["net_margin_available"] == 248500.5
    assert mock_groww.count(PATH) == 2


async def test_permanent_errors_are_not_retried(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.json(PATH, load_fixture("bad_request.json"))
    with pytest.raises(GrowwBadRequestError):
        await groww_client.get(Endpoints.MARGIN.resolve())
    assert mock_groww.count(PATH) == 1


async def test_retry_gives_up_after_max_attempts(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.json(PATH, load_fixture("internal_error.json"))
    with pytest.raises(TransientError):
        await groww_client.get(Endpoints.MARGIN.resolve())
    assert mock_groww.count(PATH) == 3  # GROWW_MAX_RETRIES


async def test_timeouts_map_to_our_taxonomy(groww_client, mock_groww: MockGroww) -> None:
    mock_groww.timeout(PATH, times=99)
    with pytest.raises((TimeoutError_, TransientError)):
        await groww_client.get(Endpoints.MARGIN.resolve(), allow_retry=False)


async def test_connection_failures_map_to_our_taxonomy(
    groww_client, mock_groww: MockGroww
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    mock_groww.on(PATH, explode)
    with pytest.raises(ConnectionFailedError):
        await groww_client.get(Endpoints.MARGIN.resolve(), allow_retry=False)


async def test_non_json_body_is_an_error_not_a_none(
    groww_client, mock_groww: MockGroww
) -> None:
    mock_groww.on(PATH, lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(Exception) as excinfo:
        await groww_client.get(Endpoints.MARGIN.resolve(), allow_retry=False)
    assert "non-JSON" in str(excinfo.value)


# --- GRW-006: the order path is never blindly retried ---------------------


@pytest.mark.safety
async def test_order_timeout_no_blind_retry(groww_client, mock_groww: MockGroww) -> None:
    """A timed-out order must be attempted exactly once by the client."""
    create_path = "/v1/order/create"
    mock_groww.timeout(create_path, times=99)

    with pytest.raises((TimeoutError_, TransientError)):
        await groww_client.post(
            Endpoints.ORDER_CREATE.resolve(),
            category=RateCategory.ORDERS,
            json={"trading_symbol": "WIPRO"},
            allow_retry=False,
        )

    # Exactly one attempt: resending could double the position.
    assert mock_groww.count(create_path) == 1


@pytest.mark.safety
async def test_transient_error_on_an_order_is_not_retried(
    groww_client, mock_groww: MockGroww
) -> None:
    create_path = "/v1/order/create"
    mock_groww.json(create_path, load_fixture("internal_error.json"))

    with pytest.raises(GrowwTransientError):
        await groww_client.post(
            Endpoints.ORDER_CREATE.resolve(),
            category=RateCategory.ORDERS,
            json={},
            allow_retry=False,
        )
    assert mock_groww.count(create_path) == 1


async def test_with_retry_honours_allow_retry(fake_clock: FakeClock) -> None:
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        raise TransientError("temporary")

    with pytest.raises(TransientError):
        await with_retry(
            flaky,
            policy=RetryPolicy(max_attempts=5, base_delay_seconds=0),
            allow_retry=False,
            clock=fake_clock,
        )
    assert attempts["count"] == 1


async def test_with_retry_respects_the_total_deadline(fake_clock: FakeClock) -> None:
    async def always_fails() -> None:
        raise TransientError("temporary")

    with pytest.raises(TransientError):
        await with_retry(
            always_fails,
            policy=RetryPolicy(
                max_attempts=10, base_delay_seconds=0.01, total_deadline_seconds=0.0
            ),
            clock=fake_clock,
        )


# --- AUTH-001…AUTH-003 ----------------------------------------------------


async def test_groww_auth_flows(settings_env) -> None:
    """TOTP is preferred when both flows are configured (its token never expires)."""
    api_only = settings_env(GROWW_API_KEY="k", GROWW_API_SECRET="s")
    assert api_only.groww_auth_flow == "api_key"

    totp_only = settings_env(GROWW_TOTP_TOKEN="t", GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP")
    assert totp_only.groww_auth_flow == "totp"

    both = settings_env(
        GROWW_API_KEY="k",
        GROWW_API_SECRET="s",
        GROWW_TOTP_TOKEN="t",
        GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP",
    )
    assert both.groww_auth_flow == "totp"


async def test_token_cache_and_refresh(
    groww_settings, token_exchange: StubTokenExchange, fake_clock: FakeClock
) -> None:
    limiter = RateLimiter(clock=fake_clock)
    auth = GrowwAuthenticator(
        groww_settings, exchange=token_exchange, clock=fake_clock, rate_limiter=limiter
    )

    first = await auth.get_token()
    second = await auth.get_token()
    assert first == second
    assert token_exchange.calls == 1  # cached

    auth.invalidate()
    third = await auth.get_token()
    assert third != first
    assert token_exchange.calls == 2


async def test_expiring_token_is_refreshed(groww_settings, fake_clock: FakeClock) -> None:
    exchange = StubTokenExchange(
        expires_at=datetime(2026, 1, 5, 23, 59, 59, tzinfo=IST)
    )
    auth = GrowwAuthenticator(
        groww_settings,
        exchange=exchange,
        clock=fake_clock,
        rate_limiter=RateLimiter(clock=fake_clock),
    )
    await auth.get_token()
    assert exchange.calls == 1

    fake_clock.advance(timedelta(hours=15))  # past end of day
    await auth.get_token()
    assert exchange.calls == 2


@pytest.mark.safety
async def test_token_budget_guard(
    settings_env, token_exchange: StubTokenExchange, fake_clock: FakeClock
) -> None:
    """The 150/24h token cap is enforced locally, not discovered remotely."""
    settings = settings_env(
        GROWW_TOTP_TOKEN="t",
        GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP",
        GROWW_DAILY_TOKEN_BUDGET="3",
    )
    auth = GrowwAuthenticator(
        settings,
        exchange=token_exchange,
        clock=fake_clock,
        rate_limiter=RateLimiter(clock=fake_clock),
    )

    for _ in range(3):
        auth.invalidate()
        await auth.get_token()
    assert auth.token_requests_today == 3

    auth.invalidate()
    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_token()
    assert "budget" in str(excinfo.value)

    # The counter rolls over with the trading day.
    fake_clock.advance(timedelta(days=1))
    assert auth.token_requests_today == 0
    await auth.get_token()


async def test_unauthorised_triggers_exactly_one_reauth(
    groww_client, mock_groww: MockGroww, token_exchange: StubTokenExchange
) -> None:
    """A rejected token re-authenticates once and replays the call once."""
    mock_groww.sequence(
        PATH,
        [
            httpx.Response(200, json=load_fixture("unauthorised.json")),
            httpx.Response(200, json=load_fixture("margin.json")),
        ],
    )

    payload = await groww_client.get(Endpoints.MARGIN.resolve())
    assert payload["net_margin_available"] == 248500.5
    assert token_exchange.calls == 2  # initial + one refresh
    assert mock_groww.count(PATH) == 2


@pytest.mark.safety
async def test_persistent_unauthorised_does_not_loop(
    groww_client, mock_groww: MockGroww, token_exchange: StubTokenExchange
) -> None:
    """Credentials that stay rejected must not burn the daily token budget."""
    mock_groww.json(PATH, load_fixture("unauthorised.json"))

    with pytest.raises(GrowwAuthError):
        await groww_client.get(Endpoints.MARGIN.resolve())

    assert token_exchange.calls == 2  # initial + exactly one refresh
    assert mock_groww.count(PATH) == 2


# --- GRW-024: logging -----------------------------------------------------


async def test_groww_request_logging_redaction(
    groww_client, mock_groww: MockGroww, caplog
) -> None:
    from app.core.logging import register_secret

    register_secret("test-access-token-1")
    mock_groww.fixture(PATH, "margin.json")

    with caplog.at_level("INFO", logger="ats.broker"):
        await groww_client.get(Endpoints.MARGIN.resolve())

    records = [r for r in caplog.records if r.name == "ats.broker"]
    assert records, "broker traffic must be logged"
    record = records[-1]
    assert record.path == "/margins/detail/user"
    assert record.status_code == 200
    assert record.rate_category == "non_trading"
    assert isinstance(record.latency_ms, int)

    # No token anywhere in the emitted text.
    assert "test-access-token-1" not in caplog.text


# --- Endpoint confidence --------------------------------------------------


def test_documented_endpoints_are_marked_documented() -> None:
    assert Endpoints.TOKEN.confidence is Confidence.DOCUMENTED
    assert Endpoints.ORDER_CREATE.confidence is Confidence.DOCUMENTED
    assert Endpoints.ORDER_DETAIL.confidence is Confidence.DOCUMENTED


def test_inferred_endpoints_are_enumerable_for_the_limitations_doc() -> None:
    """Every unverified path must be listable, so it can be documented."""
    inferred = inferred_endpoints()
    assert inferred
    paths = {endpoint.path for endpoint in inferred}
    assert "/order/modify" in paths
    assert "/live-data/quote" in paths
