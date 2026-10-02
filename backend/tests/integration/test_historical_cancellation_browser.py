"""Actual React cancellation, API audit and isolated child termination."""

import asyncio
import os
from pathlib import Path

import pytest

from app.backtest import launcher
from tests.integration.test_auth import credentials
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_jobs import plan_files
from tests.integration.test_historical_publication import catalog_database, select_target
from tests.integration.test_historical_session import full_session
from tests.integration.test_paper_browser import live_dashboard

__all__ = ["catalog_database", "credentials", "isolated_database", "live_dashboard"]
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_dashboard_cancels_local_research_and_reloads_terminal_state(
    isolated_database, catalog_database, credentials, tmp_path, monkeypatch, live_dashboard
):
    registry = plan_files(tmp_path, isolated_database, recorded=full_session())
    credentials[0].historical_plans_file = registry
    select_target(monkeypatch, catalog_database)
    children = []
    original = launcher.asyncio.create_subprocess_exec

    async def observe(*args, **kwargs):
        process = await original(*args, **kwargs)
        if "app.backtest.child" in args:
            children.append(process)
        return process

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", observe)
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "frontend/tests/historical-cancellation-browser.mjs"),
        live_dashboard,
        env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"HISTORICAL_CANCELLATION_VERIFIED\n"
        assert children and all(child.returncode is not None for child in children)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
