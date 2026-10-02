"""Committed PAPER orders trigger authenticated, read-only dashboard refreshes."""

import asyncio
import json
import os

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.api import workspace_stream
from app.core.events import RedisStreamEventBus
from app.monitoring.runtime_events import RuntimeEventPublisher
from app.security.auth import OwnerAuth
from tests.integration.test_auth import credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_redis_events import URL, redis_events

__all__ = ["credentials", "live_dashboard", "redis_events"]


async def message(socket, kind):
    async def receive():
        while True:
            result = json.loads(await socket.recv())
            assert set(result) == {"type"}
            if result["type"] == kind:
                return result

    return await asyncio.wait_for(receive(), 10)


@pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")
async def test_committed_order_refresh_reconnect_and_revocation(
    db_engine, credentials, fake_clock, redis_events, live_dashboard, monkeypatch
):
    client, prefix = redis_events
    credentials[0].redis_url = URL
    monkeypatch.setattr(workspace_stream, "STREAMS", tuple(
        f"{prefix}:{suffix}" for suffix in ("execution.order_update", "execution.order_submitted")
    ))
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    auth = OwnerAuth()
    token, _refresh = await auth.login("owner", credentials[1])
    principal = await auth.authenticate(token)
    url = live_dashboard.replace("http://", "ws://") + "/api/v1/workspace/stream"
    publisher = RuntimeEventPublisher(
        engine.settings, bus=RedisStreamEventBus(client, stream_prefix=prefix), clock=fake_clock
    )
    try:
        async with connect(url, origin=live_dashboard) as socket:
            await socket.send(json.dumps({"token": token}))
            await message(socket, "WORKSPACE_REFRESH")
            order = await engine.submit(proposal)
            assert await publisher.publish_once() >= 3
            await message(socket, "WORKSPACE_REFRESH")
            response = await api.get("/api/v1/workspace")
            assert response.status_code == 200
            assert any(row["id"] == order for row in response.json()["orders"])
            await message(socket, "WORKSPACE_HEARTBEAT")
            for stream in workspace_stream.STREAMS:
                for _identifier, fields in await client.xrange(stream):
                    await client.xadd(stream, fields)
            for _attempt in range(3):
                result = json.loads(await asyncio.wait_for(socket.recv(), 5))
                assert result == {"type": "WORKSPACE_HEARTBEAT"}
            assert len(await engine.broker.list_orders()) == 1
        async with connect(url, origin=live_dashboard) as socket:
            await socket.send(json.dumps({"token": token}))
            await message(socket, "WORKSPACE_REFRESH")
            await auth.logout(principal)
            with pytest.raises(ConnectionClosed):
                while True:
                    await asyncio.wait_for(socket.recv(), 5)
            assert socket.close_code == 4401
    finally:
        await api.aclose()


async def test_workspace_stream_rejects_unsafe_access(db_engine, credentials, live_dashboard):
    url = live_dashboard.replace("http://", "ws://") + "/api/v1/workspace/stream"
    for origin, suffix in [("https://untrusted.invalid", ""), (live_dashboard, "?token=secret")]:
        with pytest.raises(InvalidStatus):
            async with connect(url + suffix, origin=origin):
                pytest.fail("unsafe upgrade accepted")
    async with connect(url, origin=live_dashboard) as socket:
        await socket.send(json.dumps({"token": "invalid"}))
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(socket.recv(), 5)
        assert socket.close_code == 4401


@pytest.mark.skipif(not URL or os.environ.get("ATS_TEST_BROWSER") != "1", reason="enable Redis/browser")
async def test_browser_refreshes_committed_order_without_polling(
    db_engine, credentials, fake_clock, redis_events, live_dashboard, monkeypatch
):
    client, prefix = redis_events
    credentials[0].redis_url = URL
    monkeypatch.setattr(workspace_stream, "STREAMS", tuple(
        f"{prefix}:{suffix}" for suffix in ("execution.order_update", "execution.order_submitted")
    ))
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node", str(ROOT / "frontend/tests/workspace-stream-browser.mjs"), live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert await asyncio.wait_for(process.stdout.readline(), 45) == b"STREAM_READY\n"
        order = await engine.submit(proposal)
        publisher = RuntimeEventPublisher(
            engine.settings, bus=RedisStreamEventBus(client, stream_prefix=prefix), clock=fake_clock
        )
        assert await publisher.publish_once() >= 3
        process.stdin.write((order + "\n").encode())
        await process.stdin.drain()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"ORDER_STREAM_BROWSER_VERIFIED\n"
        assert len(await engine.broker.list_orders()) == 1
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await api.aclose()


@pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")
async def test_unavailable_stream_does_not_claim_connected(
    db_engine, credentials, redis_events, live_dashboard, monkeypatch
):
    client, prefix = redis_events
    credentials[0].redis_url = URL
    key = f"{prefix}:invalid-workspace-type"
    await client.set(key, "not a stream")
    monkeypatch.setattr(workspace_stream, "STREAMS", (key,))
    token, _refresh = await OwnerAuth().login("owner", credentials[1])
    url = live_dashboard.replace("http://", "ws://") + "/api/v1/workspace/stream"
    async with connect(url, origin=live_dashboard) as socket:
        await socket.send(json.dumps({"token": token}))
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(socket.recv(), 10)
        assert socket.close_code == 1011
