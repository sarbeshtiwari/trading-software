"""Actual browser admission into the persisted report store; source text is a test fixture."""

import asyncio
import os

import pytest

from app.db import session as db_session
from app.fno.restrictions import state_at
from tests.integration.test_auth import credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_browser_admits_source_report(db_engine, credentials, fake_clock, live_dashboard):
    _engine, _proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/fno-ban-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"FNO_BAN_BROWSER_VERIFIED\n"
        async with db_session.session_scope() as session:
            current = await state_at(session, "SYNTHETIC")
            assert current.status == "AVAILABLE" and current.report.underlyings == ("TEST",)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
