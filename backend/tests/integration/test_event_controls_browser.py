"""Real Risk view changes owner-published calendar controls through authenticated APIs."""

import asyncio
import os

import pytest
import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from tests.integration.test_auth import credentials
from tests.integration.test_event_controls import PATH, publication
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_browser_reviews_event_control(db_engine, credentials, fake_clock, live_dashboard):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        assert (await client.put(PATH, json=publication(fake_clock))).status_code == 200
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/event-controls-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"EVENT_CONTROLS_BROWSER_VERIFIED\n"
        async with db_session.session_scope() as session:
            events = (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.event_type == "EVENT_CONTROL_PUBLISHED")
                    .order_by(AuditEvent.sequence)
                )
            ).all()
            assert [event.data_used["enabled"] for event in events] == [True, False]
            assert all(event.actor == "owner" for event in events)
        await engine.submit(proposal)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
