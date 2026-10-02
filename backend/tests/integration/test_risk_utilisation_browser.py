"""Actual dashboard polling over the existing PAPER OMS risk observations."""

import asyncio
import os
from datetime import timedelta

import pytest

from app.risk.audit import RiskAudit
from tests.integration.test_paper_browser import ROOT, credentials, live_dashboard
from tests.integration.test_paper_execution import setup_execution
from tests.unit.test_risk import limits, market, portfolio
from tests.unit.test_risk import proposal as risk_proposal

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_risk_utilisation_polling(db_engine, credentials, fake_clock, live_dashboard):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        await engine.submit(proposal)
        await engine.monitor_once()
        rejected_id, _decision = await RiskAudit().evaluate_and_record(
            risk_proposal(quantity=251), portfolio(), market(), limits()
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/risk-utilisation-browser.mjs"),
            live_dashboard,
            rejected_id,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        first = await asyncio.wait_for(process.stdout.readline(), timeout=45)
        if first != b"RISK_AVAILABLE\n":
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            pytest.fail(stderr.decode(errors="replace"))
        fake_clock.advance(timedelta(seconds=61))
        await engine.safety.trip_error("PAPER", "SYNTHETIC")
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"RISK_STALE_AND_LATCHED\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
