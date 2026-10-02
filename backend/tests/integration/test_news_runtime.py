"""Actual APScheduler callback and durable acquisition/outbox restart paths."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import sqlalchemy as sa

from app.audit.service import AuditService
from app.config import get_settings
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsItem
from app.main import create_app
from app.monitoring.gate import get_trading_gate
from app.news.fetch import PublicFeedFetcher
from app.news.polling import NewsPoller, poll_chain
from app.news.providers import RemoteNewsProvider
from app.news.runtime import NewsAcquisitionCheck, NewsRuntime
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_controls import body
from tests.integration.test_news_ingest import configured
from tests.unit.test_news_fetch import Chunks, rss

__all__ = ["credentials", "journal_client"]


def runtime(handler, clock):
    provider = RemoteNewsProvider(
        PublicFeedFetcher(
            resolver=AsyncMock(return_value="93.184.215.14"), transport=httpx.MockTransport(handler)
        ),
        data_origin=DataOrigin.SYNTHETIC,
    )
    return NewsRuntime(
        get_settings().model_copy(update={"news_polling_enabled": True}),
        poller=NewsPoller(provider, clock=clock),
        clock=clock,
    )


async def test_actual_scheduled_callback_persists_and_restart_does_not_repoll(
    journal_client, fake_clock
):
    await configured(journal_client)
    fetched = asyncio.Event()

    async def handler(_request):
        fetched.set()
        return httpx.Response(200, headers={"Content-Type": "text/xml"}, stream=Chunks([rss()]))

    worker = runtime(handler, fake_clock)
    try:
        await worker.start()
        await asyncio.wait_for(fetched.wait(), timeout=5)
        async with worker.lock:
            assert worker.processed == 1 and worker.status == "RUNNING"
        assert worker.scheduler.get_job("news-acquisition") is not None
    finally:
        await worker.stop()
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 1
    await db_session.dispose_engine()
    db_session.init_engine()
    never = AsyncMock()
    restarted = runtime(never, fake_clock)
    restarted.running = True
    try:
        await restarted.cycle()
        never.assert_not_called()
        assert restarted.status == "RUNNING"
        assert restarted.snapshot()["actionable_news"] == "UNAVAILABLE"
        assert len(await AuditService().chain(poll_chain("fixture"))) == 2
    finally:
        await restarted.stop()


async def test_failure_notifies_durably_and_never_modifies_trading_gate(journal_client, fake_clock):
    await configured(journal_client)
    handler = AsyncMock(side_effect=httpx.ConnectError("secret-shaped remote failure"))
    worker = runtime(handler, fake_clock)
    worker.running = True
    before = get_trading_gate().state.reasons
    try:
        await worker.cycle()
        assert worker.status == "DEGRADED"
        await worker.cycle()
        assert worker.status == "DEGRADED" and handler.await_count == 1
        assert get_trading_gate().state.reasons == before
        health = await NewsAcquisitionCheck(worker).execute()
        assert health.status.value == "DEGRADED" and not health.critical
        async with db_session.session_scope() as session:
            requests = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "NOTIFICATION_REQUESTED"
                        )
                    )
                ).all()
            )
            assert len(requests) == 1
            assert requests[0].result["notification"]["message"] == "NEWS SERVICE DEGRADED: fixture"
        fake_clock.advance(timedelta(seconds=1001))
        assert worker.snapshot()["status"] == "STALE"
    finally:
        await worker.stop()


async def test_opt_in_and_shutdown_cancel_active_fetch(journal_client, fake_clock):
    await configured(journal_client)
    default = NewsRuntime()
    await default.start()
    assert not default.running and default.status == "DISABLED"
    entered = asyncio.Event()

    async def wait_forever(_request):
        entered.set()
        await asyncio.Event().wait()

    worker = runtime(wait_forever, fake_clock)
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    await asyncio.wait_for(worker.stop(), timeout=5)
    assert not worker.running and worker.status == "STOPPED"
    history = await AuditService().chain(poll_chain("fixture"))
    assert len(history) == 1 and history[0].event_type == "NEWS_POLL_STARTED"


async def test_application_lifespan_starts_scheduler_and_exposes_actual_state(
    journal_client, fake_clock
):
    await configured(journal_client)
    acquired = asyncio.Event()

    async def handler(_request):
        acquired.set()
        return httpx.Response(200, headers={"Content-Type": "text/xml"}, stream=Chunks([rss()]))

    configured_runtime = runtime(handler, fake_clock)
    app = create_app(configured_runtime.settings)
    app.state.news_runtime.poller = configured_runtime.poller
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(acquired.wait(), timeout=5)
        async with app.state.news_runtime.lock:
            assert app.state.news_runtime.processed == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=dict(journal_client.headers),
        ) as client:
            response = await client.get("/api/v1/news/runtime")
            assert response.status_code == 200
            assert response.json()["status"] == "RUNNING"
            assert response.json()["actionable_news"] == "UNAVAILABLE"
    assert app.state.news_runtime.status == "STOPPED"


async def test_bounded_source_iteration_does_not_starve_later_sources(journal_client, fake_clock):
    await configured(journal_client)
    second = body()
    second["configuration"]["is_enabled"] = True
    assert (
        await journal_client.put("/api/v1/news/sources/zz-fixture", json=second)
    ).status_code == 200

    async def response(_request):
        return httpx.Response(200, headers={"Content-Type": "text/xml"}, stream=Chunks([rss()]))

    handler = AsyncMock(side_effect=response)
    worker = runtime(handler, fake_clock)
    worker.settings = worker.settings.model_copy(update={"news_poll_batch_size": 1})
    worker.running = True
    try:
        await worker.cycle()
        assert handler.await_count == 1 and worker.cursor == "fixture"
        await worker.cycle()
        assert handler.await_count == 2 and worker.cursor == "zz-fixture"
        await worker.cycle()
        await worker.cycle()
        assert handler.await_count == 2
    finally:
        await worker.stop()


async def test_scheduler_start_failure_stays_noncritical(journal_client, fake_clock, monkeypatch):
    worker = runtime(AsyncMock(), fake_clock)
    before = get_trading_gate().state.reasons
    monkeypatch.setattr(worker.scheduler, "start", Mock(side_effect=RuntimeError("Test failure")))
    await worker.start()
    assert worker.status == "UNAVAILABLE" and not worker.running
    assert get_trading_gate().state.reasons == before
