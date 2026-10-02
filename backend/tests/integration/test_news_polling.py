"""Remote fixture → real fetch/parser/poller → immutable observations → authenticated API."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa

from app.api import news
from app.audit.service import AuditService
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.news.fetch import PublicFeedFetcher
from app.news.polling import NewsPoller, poll_chain
from app.news.providers import RemoteNewsProvider
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_controls import body
from tests.integration.test_news_ingest import configured
from tests.unit.test_news_fetch import Chunks, rss

__all__ = ["credentials", "journal_client"]


def install_provider(monkeypatch, handler):
    provider = RemoteNewsProvider(
        PublicFeedFetcher(
            resolver=AsyncMock(return_value="93.184.215.14"), transport=httpx.MockTransport(handler)
        ),
        data_origin=DataOrigin.SYNTHETIC,
    )
    monkeypatch.setattr(news, "NewsPoller", lambda: NewsPoller(provider))


async def test_actual_remote_pipeline_restart_rate_limit_and_read(
    journal_client, fake_clock, monkeypatch
):
    client = journal_client
    request = await configured(client)
    handler = AsyncMock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "application/rss+xml"}, stream=Chunks([rss()])
        )
    )
    install_provider(monkeypatch, handler)
    path = "/api/v1/news/sources/fixture"
    assert (await client.get(path + "/acquisition")).json()["status"] == "NOT_POLLED"
    response = await client.post(
        path + "/poll", json={"expected_event_id": request["expected_event_id"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ACQUIRED_UNVERIFIED"
    receipt = result["articles"][0]
    assert (
        receipt["acquisition"] == "REMOTE_FEED" and receipt["verification_status"] == "UNVERIFIED"
    )
    read = await client.get(f"/api/v1/news/articles/{receipt['article_id']}")
    assert read.status_code == 200 and read.json()["article"]["data_origin"] == "SYNTHETIC"
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await client.get(path + "/acquisition")).json()["status"] == "ACQUIRED_UNVERIFIED"
    assert (
        await client.post(path + "/poll", json={"expected_event_id": request["expected_event_id"]})
    ).status_code == 409
    assert handler.await_count == 1
    fake_clock.advance(timedelta(seconds=301))
    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    client.headers["Authorization"] = f"Bearer {refreshed.json()['access_token']}"
    assert (await client.get(path + "/acquisition")).json()["status"] == "STALE"
    assert await AuditService().verify(poll_chain("fixture"))


async def test_failure_rolls_back_whole_batch_and_records_degradation(journal_client, monkeypatch):
    client = journal_client
    request = await configured(client)
    bad_batch = rss().replace(
        b"</channel>",
        rss()
        .split(b"<item>")[1]
        .split(b"</item>")[0]
        .join([b"<item>", b"</item>"])
        .replace(b"/story", b"/different")
        .replace(b"21 Sep 2026", b"21 Sep 2099")
        + b"</channel>",
    )
    handler = AsyncMock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/xml"}, stream=Chunks([bad_batch])
        )
    )
    install_provider(monkeypatch, handler)
    response = await client.post(
        "/api/v1/news/sources/fixture/poll",
        json={"expected_event_id": request["expected_event_id"]},
    )
    assert response.status_code == 200 and response.json()["status"] == "DEGRADED"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 0
    history = await AuditService().chain(poll_chain("fixture"))
    assert len(history) == 2 and history[-1].result["status"] == "DEGRADED"


async def test_concurrent_poll_and_cancelled_request_remain_durably_rate_limited(
    journal_client, fake_clock, monkeypatch
):
    request = await configured(journal_client)
    started = asyncio.Event()

    async def hanging(_request):
        started.set()
        await asyncio.Event().wait()

    install_provider(monkeypatch, hanging)
    path = "/api/v1/news/sources/fixture"
    payload = {"expected_event_id": request["expected_event_id"]}
    task = asyncio.create_task(journal_client.post(path + "/poll", json=payload))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert (await journal_client.get(path + "/acquisition")).json()["status"] == "PENDING"
        assert (await journal_client.post(path + "/poll", json=payload)).status_code == 409
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await db_session.dispose_engine()
    db_session.init_engine()
    fake_clock.advance(timedelta(seconds=21))
    assert (await journal_client.get(path + "/acquisition")).json()["status"] == "DEGRADED"
    assert (await journal_client.post(path + "/poll", json=payload)).status_code == 409


async def test_source_change_during_fetch_discards_batch(journal_client, monkeypatch):
    request = await configured(journal_client)

    async def change_source(_request):
        update = body(expected_event_id=request["expected_event_id"])
        assert (
            await journal_client.put("/api/v1/news/sources/fixture", json=update)
        ).status_code == 200
        return httpx.Response(200, headers={"Content-Type": "text/xml"}, stream=Chunks([rss()]))

    install_provider(monkeypatch, change_source)
    response = await journal_client.post(
        "/api/v1/news/sources/fixture/poll",
        json={"expected_event_id": request["expected_event_id"]},
    )
    assert response.status_code == 200 and response.json()["status"] == "DEGRADED"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 0


async def test_empty_feed_cannot_backdate_completion(journal_client, fake_clock, monkeypatch):
    request = await configured(journal_client)

    async def regress_clock(_request):
        fake_clock.advance(timedelta(seconds=-1))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml"},
            stream=Chunks([b'<rss version="2.0"><channel/></rss>']),
        )

    install_provider(monkeypatch, regress_clock)
    response = await journal_client.post(
        "/api/v1/news/sources/fixture/poll",
        json={"expected_event_id": request["expected_event_id"]},
    )
    assert response.status_code == 409
    history = await AuditService().chain(poll_chain("fixture"))
    assert len(history) == 1 and history[0].event_type == "NEWS_POLL_STARTED"


async def test_configured_frontend_origin_can_preflight_put_but_unknown_origin_cannot(
    journal_client,
):
    headers = {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type,x-requested-with",
    }
    response = await journal_client.options("/api/v1/news/sources/fixture", headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == headers["Origin"]
    headers["Origin"] = "https://untrusted.example.test"
    assert (
        await journal_client.options("/api/v1/news/sources/fixture", headers=headers)
    ).status_code == 400
