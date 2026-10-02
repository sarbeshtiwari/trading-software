"""Real network WebSocket with actual owner sessions, ingestion and audit reads."""

import asyncio
import json
from datetime import timedelta

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.api import market_stream
from app.security.auth import OwnerAuth
from app.trading.observations import ReferenceIngestion
from tests.integration.test_auth import credentials
from tests.integration.test_fundamentals import seed
from tests.integration.test_paper_browser import live_dashboard
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_option_chain import OBSERVED

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.parametrize("revoked", [True, False])
async def test_stream_auth_freshness_and_revocation(
    db_engine, credentials, fake_clock, live_dashboard, revoked
):
    await seed()
    fake_clock.set_to(OBSERVED)
    await ReferenceIngestion(fixture_provider(), clock=fake_clock).observe_quote("ins-test")
    auth = OwnerAuth()
    token, _refresh = await auth.login("owner", credentials[1])
    principal = await auth.authenticate(token)
    url = live_dashboard.replace("http://", "ws://") + "/api/v1/market/stream"
    async with connect(url, origin=live_dashboard) as socket:
        await socket.send(
            json.dumps({"token": token, "instruments": ["ins-test"], "origin": "SYNTHETIC"})
        )
        current = json.loads(await asyncio.wait_for(socket.recv(), 5))
        assert current["quotes"][0]["ltp"] == "100"
        fake_clock.advance(timedelta(seconds=20))
        stale = json.loads(await asyncio.wait_for(socket.recv(), 5))
        assert stale["quotes"][0]["status"] == "STALE"
        assert stale["quotes"][0]["ltp"] is None
        if revoked:
            await auth.logout(principal)
        else:
            fake_clock.advance(timedelta(seconds=41))
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(socket.recv(), 5)
        assert socket.close_code == 4401


async def test_stream_refuses_unsafe_handshakes(db_engine, credentials, live_dashboard):
    url = live_dashboard.replace("http://", "ws://") + "/api/v1/market/stream"
    for origin, suffix in [("https://untrusted.invalid", ""), (live_dashboard, "?token=refused")]:
        with pytest.raises(InvalidStatus):
            async with connect(url + suffix, origin=origin):
                pytest.fail("unsafe upgrade accepted")
    for body in [
        {"token": "invalid", "instruments": ["ins-test"], "origin": "LIVE"},
        {"token": "invalid", "instruments": ["ins-test"] * 11, "origin": "LIVE"},
    ]:
        async with connect(url, origin=live_dashboard) as socket:
            await socket.send(json.dumps(body))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(socket.recv(), 5)
            assert socket.close_code in (4401, 1008)
    await asyncio.sleep(0)
    assert not market_stream._connections
