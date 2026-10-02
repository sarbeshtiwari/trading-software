import asyncio
from unittest.mock import AsyncMock

import pytest

from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.ratelimit import RateLimiter
from app.core.errors import RateLimitError


@pytest.mark.parametrize(
    "failure", [RuntimeError("isolated exchange failure"), asyncio.CancelledError()]
)
async def test_failed_or_cancelled_exchange_consumes_attempt_budget(
    settings_env, fake_clock, failure
):
    settings = settings_env(GROWW_DAILY_TOKEN_BUDGET="1")
    exchange = AsyncMock()
    exchange.exchange.side_effect = failure
    auth = GrowwAuthenticator(
        settings, exchange=exchange, clock=fake_clock, rate_limiter=RateLimiter(clock=fake_clock)
    )
    with pytest.raises(type(failure)):
        await auth.get_token()
    assert auth.token_requests_today == 1
    assert auth.cached_token is None
    with pytest.raises(RateLimitError):
        await auth.get_token()
    exchange.exchange.assert_awaited_once()
