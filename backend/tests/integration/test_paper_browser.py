"""Opt-in real browser over the production React bundle and actual PAPER services."""

import asyncio
import os
import socket
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.core.calendar import TradingCalendar
from app.main import create_app
from app.trading.worker import PaperWorker, _runtime
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_jobs import plan_files
from tests.integration.test_historical_options import recorded_option
from tests.integration.test_historical_publication import catalog_database, select_target
from tests.integration.test_news_polling import install_provider
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.integration.test_walkforward import experiment_files
from tests.integration.test_walkforward_reports import two_windows
from tests.unit.test_news_fetch import Chunks, rss

__all__ = ["catalog_database", "credentials", "isolated_database"]
ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
@pytest.mark.parametrize("bulk", [False, True])
async def test_real_browser_cancels_partial_entry(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, live_dashboard, bulk
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "owner-cancel-browser.lock",
    )
    monkeypatch.setattr(_runtime, "worker", worker)
    process = None
    try:
        await worker.start(schedule=False)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/order-cancel-browser.mjs"),
            live_dashboard,
            identifier,
            "bulk" if bulk else "single",
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"OWNER_CANCEL_VERIFIED\n"
        assert len(await engine.broker.list_orders()) == 1
        await engine.verify_protection()
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await worker.stop()
        await client.aclose()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_real_browser_configures_news_without_claiming_ingestion(
    db_engine,
    credentials,
    live_dashboard,
    monkeypatch,
):
    install_provider(
        monkeypatch,
        AsyncMock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": "application/rss+xml"},
                stream=Chunks(
                    [
                        rss()
                        .replace(b"publisher.example.test", b"fixture.example.test")
                        .replace(b"21 Sep 2026", b"21 Sep 2025")
                    ]
                ),
            )
        ),
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "frontend/tests/news-sources-browser.mjs"),
        live_dashboard,
        env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"NEWS_SOURCE_CONFIGURATION_VERIFIED\n"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_real_worker_degradation_reaches_browser(
    db_engine, credentials, fake_clock, tmp_path, live_dashboard
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    process = None
    try:
        response = await client.post(
            "/api/v1/strategies/closed-candle-breakout/degradation/policy",
            json={
                "version": "1",
                "reason": "Owner configures isolated browser degradation test",
                "policy": {
                    "version": 1,
                    "window_trades": 10,
                    "minimum_trades": 1,
                    "maximum_drawdown_amount": "100",
                    "data_origin": "SYNTHETIC",
                },
            },
        )
        assert response.status_code == 200, response.text
        await worker.cycle()
        assert not worker.failed, worker.detail
        quote = provider.get_quote.return_value
        fake_clock.advance(timedelta(seconds=1))
        provider.get_quote.return_value = replace(
            quote,
            observed_at=fake_clock.now(),
            ltp=Decimal(96),
            bids=(replace(quote.bids[0], price=Decimal(96)),),
            asks=(replace(quote.asks[0], price=Decimal("96.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        await worker.cycle()
        assert not worker.failed, worker.detail
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/degradation-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"DEGRADATION_VERIFIED\n"
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        await worker.stop()
        await client.aclose()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_real_browser_reads_cancellation_journal_and_eod(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, live_dashboard
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    await engine.submit(proposal)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "hygiene-browser.lock",
    )
    monkeypatch.setattr(_runtime, "worker", worker)
    process = None
    try:
        await worker.start(schedule=False)
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        market["observed"] = fake_clock.now()
        await worker.cycle()
        assert not worker.failed, worker.detail
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/order-journal-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"ORDER_JOURNAL_VERIFIED\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await worker.stop()
        await client.aclose()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_real_browser_reads_and_polls_session_summary(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, live_dashboard
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    monkeypatch.setattr(_runtime, "worker", worker)
    process = None
    try:
        await worker.cycle()
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now()
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/summary-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"SUMMARY_VERIFIED\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await worker.stop()
        await client.aclose()


@pytest.fixture
async def live_dashboard(credentials):
    assert (ROOT / "frontend/dist/index.html").exists(), "Build the frontend first"
    static = Starlette(
        routes=[Mount("/", app=StaticFiles(directory=ROOT / "frontend/dist", html=True))]
    )

    async def application(scope, receive, send):
        target = api if scope["path"].startswith("/api/") else static
        await target(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    credentials[0].cors_origins = [f"http://127.0.0.1:{port}"]
    api = create_app(credentials[0])
    server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:

        async def wait_started():
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("HTTP server stopped before starting")
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_started(), timeout=10)
        yield f"http://127.0.0.1:{port}"
    finally:
        await api.state.walkforward_jobs.stop()
        await api.state.historical_jobs.stop()
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)
        listener.close()


@pytest.mark.skipif(
    os.environ.get("ATS_TEST_BROWSER") != "1",
    reason="set ATS_TEST_BROWSER=1 for real browser verification",
)
@pytest.mark.parametrize("exit_path", ["target", "emergency", "stale_emergency"])
async def test_real_browser_observes_costed_reference_trade(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, exit_path, live_dashboard
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    monkeypatch.setattr(_runtime, "worker", worker)
    monkeypatch.setattr(_runtime, "error", None)
    process = None
    try:
        await worker.cycle()
        await worker.cycle()
        assert not worker.failed, worker.detail

        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/paper-browser.mjs"),
            live_dashboard,
            env={
                **os.environ,
                "ATS_TEST_OWNER_PASSWORD": credentials[1],
                "ATS_TEST_EXIT_PATH": exit_path,
            },
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
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
            observed_at=quote.observed_at - timedelta(seconds=100)
            if exit_path == "stale_emergency"
            else quote.observed_at,
        )
        if exit_path == "target":
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


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
@pytest.mark.parametrize("long_option", [False, True])
async def test_real_browser_observes_historical_result(
    isolated_database,
    catalog_database,
    credentials,
    fake_clock,
    tmp_path,
    live_dashboard,
    monkeypatch,
    long_option,
):
    registry = plan_files(
        tmp_path, isolated_database, recorded=recorded_option() if long_option else None
    )
    credentials[0].historical_plans_file = registry
    select_target(monkeypatch, catalog_database)
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "frontend/tests/historical-browser.mjs"),
        live_dashboard,
        env={
            **os.environ,
            "ATS_TEST_OWNER_PASSWORD": credentials[1],
            "ATS_TEST_HISTORICAL_KIND": "OPTION" if long_option else "CASH",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"HISTORICAL_VERIFIED\n"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
@pytest.mark.parametrize("long_option", [False, True])
async def test_real_browser_launches_walkforward_and_shows_oos_only(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    live_dashboard,
    long_option,
):
    registry, _ = (
        await experiment_files(tmp_path, recorded=recorded_option())
        if long_option
        else await two_windows(tmp_path)
    )
    credentials[0].historical_plans_file = registry
    process = await asyncio.create_subprocess_exec(
        "node",
        str(
            ROOT
            / "frontend/tests"
            / ("option-walkforward-browser.mjs" if long_option else "walkforward-browser.mjs")
        ),
        live_dashboard,
        env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=240)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"WALKFORWARD_VERIFIED\n"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
