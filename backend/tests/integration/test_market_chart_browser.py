"""Production chart library and actual ingestion/API; no mocked browser responses."""

import asyncio
import json
import os
from datetime import timedelta

import pytest

from app.analysis.fundamental.source import ManualJSONSource
from app.analysis.fundamental.store import FundamentalStore
from app.trading.observations import ReferenceIngestion
from tests.integration.test_market_chart import chart_data, credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_reference_ingestion import fixture_provider

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_stored_candle_chart(db_engine, credentials, fake_clock, live_dashboard):
    client, instrument = await chart_data(credentials, fake_clock)
    await ReferenceIngestion(fixture_provider(), clock=fake_clock).observe_quote(instrument)
    await FundamentalStore(fake_clock).import_source(
        ManualJSONSource(),
        json.dumps(
            [
                {
                    "instrument_id": instrument,
                    "source": "browser-fixture",
                    "known_at": fake_clock.now().isoformat(),
                    "period_end": fake_clock.now().date().isoformat(),
                    "metrics": {
                        "pe_ratio": {"value": "12.5", "as_of": fake_clock.now().isoformat()}
                    },
                }
            ]
        ),
        max_age=timedelta(days=365),
    )
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
