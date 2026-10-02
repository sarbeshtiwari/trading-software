"""Production chart library and actual ingestion/API; no mocked browser responses."""

import asyncio
import os

import pytest

from tests.integration.test_market_chart import chart_data, credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_stored_candle_chart(db_engine, credentials, fake_clock, live_dashboard):
    client, instrument = await chart_data(credentials, fake_clock)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/market-chart-browser.mjs"),
            live_dashboard,
            instrument,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"MARKET_CHART_VERIFIED\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
