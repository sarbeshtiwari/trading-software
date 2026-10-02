"""Real research scheduling and recovery; only the external Claude HTTP edge is a fixture."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.config import get_settings
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.main import create_app
from app.news.fetch import PublicFeedFetcher
from app.news.polling import NewsPoller
from app.news.production import ResearchProducer, production_chain
from app.news.providers import RemoteNewsProvider
from app.news.runtime import NewsRuntime
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_llm_service import install_http
from tests.integration.test_news_availability import news_context
from tests.integration.test_news_controls import body
from tests.integration.test_news_research_admission import QUOTE, install_interpreter, observe
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_news_fetch import Chunks
from tests.unit.test_proposal import payload

__all__ = ["credentials", "journal_client"]


def producer(clock):
    return ResearchProducer(
        get_settings().model_copy(update={"news_research_enabled": True}),
        clock=clock,
        origin=DataOrigin.SYNTHETIC,
    )


async def test_actual_scheduler_produces_admitted_decision_evidence_once(
    journal_client, fake_clock, monkeypatch
):
    context, _strategy = await news_context()
    await observe(journal_client, "primary", primary=True)
    handler = install_interpreter(monkeypatch)
    called = asyncio.Event()
    calls = []

    async def remote(request):
        calls.append(request)
        called.set()
        return handler(request)

    install_http(monkeypatch, remote)
    worker = NewsRuntime(
        get_settings().model_copy(
            update={
                "news_polling_enabled": True,
                "news_research_enabled": True,
            }
        ),
        clock=fake_clock,
    )
    worker.research = producer(fake_clock)
    try:
        await worker.start()
        await asyncio.wait_for(called.wait(), timeout=10)
        async with worker.lock:
            result = worker.research.results[0]
            assert result.status == "ADMITTED"
            assert worker.snapshot()["actionable_news"] == "PER_INSTRUMENT_CHECK_REQUIRED"
        decision = await quant_decision(
            DecisionPipeline(validator()),
            payload(evidence=[{"kind": "NEWS", "source_id": result.admission_ids[0]}]),
            context,
        )
        assert decision.code == "RISK_APPROVED"
    finally:
        await worker.stop()
    await db_session.dispose_engine()
    db_session.init_engine()
    restarted = producer(fake_clock)
    await restarted.cycle()
    assert restarted.results == [result] and len(calls) == 1
    assert len(await AuditService().chain(production_chain(result.group_event_id))) == 2


async def test_restart_during_paid_call_never_replays_unknown_outcome(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    install_interpreter(monkeypatch)
    entered = asyncio.Event()
    calls = []

    async def interrupted(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    install_http(monkeypatch, interrupted)
    task = asyncio.create_task(producer(fake_clock).produce(article_id))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db_session.dispose_engine()
    db_session.init_engine()
    result = await producer(fake_clock).produce(article_id)
    assert result.status == "RECOVERY_REQUIRED" and len(calls) == 1
    assert len(await AuditService().chain(production_chain(result.group_event_id))) == 1


async def test_failed_final_audit_rolls_back_admissions_and_recovers_saved_interpretation(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    handler = install_interpreter(monkeypatch)
    remote = AsyncMock(side_effect=handler)
    install_http(monkeypatch, remote)
    worker = producer(fake_clock)
    monkeypatch.setattr(
        worker, "_finish", AsyncMock(side_effect=RuntimeError("isolated storage failure"))
    )
    result = await worker.produce(article_id)
    assert result.status == "RECOVERY_REQUIRED"
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "NEWS_RESEARCH_ADMITTED")
            )
            == 0
        )
    await db_session.dispose_engine()
    db_session.init_engine()
    result = await producer(fake_clock).produce(article_id)
    assert result.status == "ADMITTED" and remote.await_count == 1


async def test_disabled_default_and_unavailable_provider_do_not_fabricate_research(
    journal_client, fake_clock
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    disabled = ResearchProducer(clock=fake_clock)
    await disabled.cycle()
    assert disabled.status == "DISABLED" and disabled.results == []
    with pytest.raises(ValueError, match="disabled"):
        await disabled.produce(article_id)
    worker = producer(fake_clock)
    result = await worker.produce(article_id)
    assert result.status == "UNAVAILABLE" and result.code == "PROVIDER_UNAVAILABLE"
    assert await producer(fake_clock).produce(article_id) == result


async def test_second_worker_does_not_call_provider_for_claimed_group(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    handler = install_interpreter(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def pending(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return handler(request)

    install_http(monkeypatch, pending)
    first = asyncio.create_task(producer(fake_clock).produce(article_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        second = await producer(fake_clock).produce(article_id)
        assert second.status == "RECOVERY_REQUIRED" and len(calls) == 1
    finally:
        release.set()
        completed = await first
    assert completed.status == "ADMITTED"


async def test_durable_claim_failure_prevents_paid_call(journal_client, fake_clock, monkeypatch):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    handler = install_interpreter(monkeypatch)
    remote = AsyncMock(side_effect=handler)
    install_http(monkeypatch, remote)
    append = AuditService.append_in_session

    async def fail_intent(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_PRODUCTION_STARTED":
            raise RuntimeError("Isolated intent write failure")
        return await append(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail_intent)
    with pytest.raises(RuntimeError, match="intent write failure"):
        await producer(fake_clock).produce(article_id)
    remote.assert_not_called()


async def test_feed_acquisition_to_automatic_research_and_authenticated_runtime(
    journal_client, fake_clock, monkeypatch
):
    context, _strategy = await news_context()
    for slug in ("first", "second"):
        request = body()
        request["configuration"].update(
            publisher_id=slug,
            endpoint=f"https://{slug}.example.test/feed",
            kind="api",
            is_enabled=True,
        )
        assert (
            await journal_client.put(f"/api/v1/news/sources/{slug}", json=request)
        ).status_code == 200
    install_interpreter(monkeypatch)

    def feed(request):
        host = request.headers["Host"]
        document = {
            "articles": [
                {
                    "url": f"https://{host}/article",
                    "title": "Synthetic announcement",
                    "body": QUOTE
                    + " "
                    + " ".join(f"fixture{index}" for index in range(80))
                    + " "
                    + host,
                    "published_at": fake_clock.utcnow().isoformat(),
                }
            ]
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=Chunks([json.dumps(document).encode()]),
        )

    provider = RemoteNewsProvider(
        PublicFeedFetcher(
            resolver=AsyncMock(return_value="93.184.215.14"),
            transport=httpx.MockTransport(feed),
        ),
        data_origin=DataOrigin.SYNTHETIC,
    )
    worker = NewsRuntime(
        get_settings().model_copy(
            update={
                "news_polling_enabled": True,
                "news_research_enabled": True,
            }
        ),
        poller=NewsPoller(provider),
        clock=fake_clock,
    )
    worker.research = producer(fake_clock)
    worker.running = True
    await worker.cycle()
    assert worker.processed == 2 and worker.status == "RUNNING"
    assert len(worker.research.results) == 1
    assert all(result.status == "ADMITTED" for result in worker.research.results)
    receipt = worker.research.results[0].admission_ids[0]
    decision = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": receipt}]),
        context,
    )
    assert decision.code == "RISK_APPROVED"
    app = create_app(worker.settings)
    app.state.news_runtime = worker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=dict(journal_client.headers),
    ) as client:
        response = await client.get("/api/v1/news/runtime")
    assert response.status_code == 200
    assert response.json()["research_results"][0]["admission_ids"] == [receipt]
    await worker.stop()
