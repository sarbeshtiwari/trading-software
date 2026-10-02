import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.budget import TokenAttemptBudget
from app.core.errors import RateLimitError
from app.db import session as db_session


async def test_restart_and_midnight_do_not_reset_window(db_engine, fake_clock):
    await TokenAttemptBudget(fake_clock).reserve(1)
    fake_clock.advance(timedelta(hours=18))
    with pytest.raises(RateLimitError):
        await TokenAttemptBudget(fake_clock).reserve(1)
    fake_clock.advance(timedelta(hours=6))
    await TokenAttemptBudget(fake_clock).reserve(1)


async def test_competing_reservations_cannot_exceed_budget(db_engine, fake_clock):
    outcomes = await asyncio.gather(
        *(TokenAttemptBudget(fake_clock).reserve(2) for _ in range(8)),
        return_exceptions=True,
    )
    assert outcomes.count(None) == 2
    assert all(outcome is None or isinstance(outcome, RateLimitError) for outcome in outcomes)


async def test_database_failure_prevents_external_token_call(settings_env, monkeypatch):
    def unavailable():
        raise RuntimeError("isolated database failure")

    monkeypatch.setattr(db_session, "session_scope", unavailable)
    exchange = AsyncMock()
    auth = GrowwAuthenticator(settings_env(), exchange=exchange)
    with pytest.raises(RuntimeError, match="isolated database failure"):
        await auth.get_token()
    exchange.exchange.assert_not_awaited()
