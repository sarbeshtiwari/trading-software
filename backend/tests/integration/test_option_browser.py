"""Real Edge/React over actual option worker, OMS, journal and notification state."""

import asyncio
import os
from dataclasses import replace
from decimal import Decimal

import pytest

from tests.integration.test_option_evidence import credentials, setup_option_worker
from tests.integration.test_paper_browser import ROOT, live_dashboard

__all__ = ["credentials", "live_dashboard"]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_option_lifecycle_reaches_real_dashboard(
    db_engine, credentials, fake_clock, tmp_path, live_dashboard
):
    worker, provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    process = None
    try:
        await worker.cycle()
        await worker.cycle()
        assert not worker.failed, worker.detail
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/option-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        entry = await asyncio.wait_for(process.stdout.readline(), timeout=45)
        if entry != b"ENTRY_VERIFIED\n":
            error = await asyncio.wait_for(process.stderr.read(), timeout=5)
            pytest.fail(error.decode(errors="replace"))
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(96),
            bids=(replace(quote.bids[0], price=Decimal(96)),),
            asks=(replace(quote.asks[0], price=Decimal("96.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        process.stdin.write(b"EXIT_READY\n")
        await process.stdin.drain()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"EXIT_VERIFIED\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await worker.stop()
        await client.aclose()
