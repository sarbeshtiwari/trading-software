"""Actual Redis failed handler reaches authenticated, payload-free monitoring API."""

import asyncio
import os

import httpx
import pytest

from app.api import event_failures
from app.core.events import Event, EventType
from app.main import create_app
from app.security.auth import OwnerAuth
from tests.integration.test_auth import credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_redis_events import URL, bus, redis_events

__all__ = ["credentials", "live_dashboard", "redis_events"]
pytestmark = pytest.mark.skipif(not URL, reason="set ATS_TEST_REDIS_URL")


async def test_failed_delivery_reaches_authenticated_api(
    db_engine, credentials, redis_events, monkeypatch, live_dashboard
):
    client, prefix = redis_events
    monkeypatch.setattr(credentials[0], "redis_url", URL)
    monkeypatch.setattr(event_failures, "STREAM", f"{prefix}:dead-letter")
    worker = bus(client, prefix, "monitor-test")

    async def broken(event):
        raise ValueError("test-only-sensitive-error")

    worker.subscribe(EventType.FILL, broken, name="test-only-sensitive-handler")
    await worker.start()
    try:
        await worker.publish(Event(type=EventType.FILL, payload={"secret": "test-only-payload"}))

        async def retained():
            while not await client.xlen(event_failures.STREAM):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(retained(), 5)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as api:
            assert (await api.get("/api/v1/events/failures")).status_code == 401
            access, _refresh = await OwnerAuth().login("owner", credentials[1])
            api.headers["Authorization"] = f"Bearer {access}"
            response = await api.get("/api/v1/events/failures")
            assert response.status_code == 200
            assert response.json()["retained_count"] == 1
            assert response.json()["failures"][0]["event_type"] == "execution.fill"
            assert "test-only" not in response.text
            assert "secret" not in response.text
            if os.environ.get("ATS_TEST_BROWSER") == "1":
                process = await asyncio.create_subprocess_exec(
                    "node",
                    str(ROOT / "frontend/tests/event-failures-browser.mjs"),
                    live_dashboard,
                    env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
                    assert process.returncode == 0, stderr.decode(errors="replace")
                    assert stdout == b"EVENT_FAILURE_MONITOR_VERIFIED\n"
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()
            invalid_key = f"{prefix}:invalid-monitor-type"
            assert invalid_key.startswith("ats:test:events:")
            await client.set(invalid_key, "wrong type fixture")
            monkeypatch.setattr(event_failures, "STREAM", invalid_key)
            unavailable = (await api.get("/api/v1/events/failures")).json()
            assert unavailable["status"] == "EVENT_MONITOR_UNAVAILABLE"
            assert unavailable["retained_count"] is None
    finally:
        await worker.stop()
