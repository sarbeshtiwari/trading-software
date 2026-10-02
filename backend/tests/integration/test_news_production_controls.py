"""Authenticated recovery/retry/abandon actions preserve audit and paid-call uncertainty."""

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.llm import LLMCall
from app.news.production import production_chain
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_llm_service import install_http
from tests.integration.test_news_availability import news_context
from tests.integration.test_news_production import producer
from tests.integration.test_news_research_admission import install_interpreter, observe
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.unit.test_llm import settings

__all__ = ["credentials", "journal_client", "live_dashboard"]


async def prepare(client, clock):
    await news_context()
    article_id, _version = await observe(client, "primary", primary=True)
    result = await producer(clock).produce(article_id)
    path = f"/api/v1/news/production/{result.group_event_id}"
    view = await client.get(path)
    assert view.status_code == 200, view.text
    return article_id, path, view.json()


async def control(client, path, view, action, **changes):
    return await client.post(
        path + "/control",
        json={
            "action": action,
            "expected_event_id": view["event_id"],
            "reason": "Owner reviews isolated research recovery evidence",
            **changes,
        },
    )


async def test_authorized_retry_is_single_use_durable_and_reaches_real_admission(
    journal_client, fake_clock, monkeypatch
):
    article_id, path, failed = await prepare(journal_client, fake_clock)
    assert failed["state"] == "FINISHED" and failed["result"]["code"] == "PROVIDER_UNAVAILABLE"
    authorized = await control(journal_client, path, failed, "AUTHORIZE_RETRY")
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["state"] == "RETRY_AUTHORIZED"
    assert (await control(journal_client, path, failed, "AUTHORIZE_RETRY")).status_code == 409
    handler = install_interpreter(monkeypatch)
    remote = AsyncMock(side_effect=handler)
    install_http(monkeypatch, remote)
    await db_session.dispose_engine()
    db_session.init_engine()
    result = await producer(fake_clock).produce(article_id)
    assert result.status == "ADMITTED" and remote.await_count == 1
    assert await producer(fake_clock).produce(article_id) == result
    finished = (await journal_client.get(path)).json()
    assert finished["attempts"] == 2 and finished["retry_authorizations"] == 1
    assert finished["billing_verification"] == "UNVERIFIED"
    records = await AuditService().chain(production_chain(result.group_event_id))
    assert len(records) == 5 and records[2].actor == "owner"
    assert records[3].result["authorization_id"] == records[2].id


async def test_authorization_expiry_and_retry_limit_are_enforced(journal_client, fake_clock):
    article_id, path, failed = await prepare(journal_client, fake_clock)
    authorized = await control(
        journal_client, path, failed, "AUTHORIZE_RETRY", authorization_seconds=1
    )
    assert authorized.status_code == 200
    fake_clock.advance(timedelta(seconds=1))
    result = await producer(fake_clock).produce(article_id)
    assert result.code == "RETRY_AUTHORIZATION_EXPIRED"
    expired = (await journal_client.get(path)).json()
    assert expired["state"] == "RETRY_EXPIRED" and expired["attempts"] == 1
    assert (await control(journal_client, path, expired, "AUTHORIZE_RETRY")).status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(LLMCall)) == 0


async def test_cancelled_paid_call_cannot_retry_and_abandon_does_not_erase_reservation(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    install_interpreter(monkeypatch)
    entered = asyncio.Event()

    async def pending(_request):
        entered.set()
        await asyncio.Event().wait()

    remote = AsyncMock(side_effect=pending)
    install_http(monkeypatch, remote)
    task = asyncio.create_task(producer(fake_clock).produce(article_id))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await producer(fake_clock).produce(article_id)
    path = f"/api/v1/news/production/{result.group_event_id}"
    view = (await journal_client.get(path)).json()
    assert (await control(journal_client, path, view, "AUTHORIZE_RETRY")).status_code == 409
    recovered = await control(journal_client, path, view, "RECOVER")
    assert recovered.status_code == 200 and recovered.json()["state"] == "PENDING"
    abandoned = await control(journal_client, path, recovered.json(), "ABANDON")
    assert abandoned.status_code == 200 and abandoned.json()["state"] == "ABANDONED"
    assert (await producer(fake_clock).produce(article_id)).status == "ABANDONED"
    assert remote.await_count == 1
    async with db_session.session_scope() as session:
        call = await session.scalar(sa.select(LLMCall))
        assert call.outcome == "PENDING" and call.cost_usd is None
    journal_client.headers.pop("Authorization")
    assert (await control(journal_client, path, abandoned.json(), "ABANDON")).status_code == 401


async def test_recover_saved_receipt_without_call_and_audit_actor(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    remote = AsyncMock(side_effect=install_interpreter(monkeypatch))
    install_http(monkeypatch, remote)
    worker = producer(fake_clock)
    monkeypatch.setattr(worker, "_finish", AsyncMock(side_effect=RuntimeError("Isolated crash")))
    failed = await worker.produce(article_id)
    path = f"/api/v1/news/production/{failed.group_event_id}"
    view = (await journal_client.get(path)).json()
    recovered = await control(journal_client, path, view, "RECOVER")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["result"]["status"] == "ADMITTED" and remote.await_count == 1
    records = await AuditService().chain(production_chain(failed.group_event_id))
    assert records[-2].event_type == "NEWS_PRODUCTION_RECOVERY_REQUESTED"
    assert records[-2].actor == "owner"


async def test_abandon_inflight_prevents_late_admission(journal_client, fake_clock, monkeypatch):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    handler = install_interpreter(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(request):
        entered.set()
        await release.wait()
        return handler(request)

    install_http(monkeypatch, pending)
    task = asyncio.create_task(producer(fake_clock).produce(article_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        story = (await journal_client.get(f"/api/v1/news/articles/{article_id}/story")).json()
        path = f"/api/v1/news/production/{story['event_id']}"
        view = (await journal_client.get(path)).json()
        assert (await control(journal_client, path, view, "ABANDON")).status_code == 200
    finally:
        release.set()
        result = await task
    assert result.status == "ABANDONED"
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "NEWS_RESEARCH_ADMITTED")
            )
            == 0
        )


async def test_terminal_timeout_is_not_a_safe_retry(journal_client, fake_clock, monkeypatch):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    install_interpreter(monkeypatch)
    monkeypatch.setattr(
        "app.news.interpret.get_settings", lambda: settings(llm_max_repair_attempts=0)
    )
    install_http(monkeypatch, AsyncMock(side_effect=httpx.ReadTimeout("Isolated provider timeout")))
    result = await producer(fake_clock).produce(article_id)
    assert result.status == "UNAVAILABLE" and result.code == "TIMEOUT"
    path = f"/api/v1/news/production/{result.group_event_id}"
    view = (await journal_client.get(path)).json()
    assert (await control(journal_client, path, view, "AUTHORIZE_RETRY")).status_code == 409


async def test_concurrent_authorizations_have_one_winner(journal_client, fake_clock):
    _article_id, path, failed = await prepare(journal_client, fake_clock)
    responses = await asyncio.gather(
        control(journal_client, path, failed, "AUTHORIZE_RETRY"),
        control(journal_client, path, failed, "AUTHORIZE_RETRY"),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]


async def test_control_audit_failure_rolls_back_authorization(
    journal_client, fake_clock, monkeypatch
):
    _article_id, path, failed = await prepare(journal_client, fake_clock)
    append = AuditService.append_in_session

    async def fail_after_write(self, session, identity, details, **kwargs):
        result = await append(self, session, identity, details, **kwargs)
        if identity.event_type == "NEWS_PRODUCTION_RETRY_AUTHORIZED":
            raise RuntimeError("Isolated control audit failure")
        return result

    monkeypatch.setattr(AuditService, "append_in_session", fail_after_write)
    with pytest.raises(RuntimeError, match="control audit failure"):
        await control(journal_client, path, failed, "AUTHORIZE_RETRY")
    assert (await journal_client.get(path)).json() == failed


async def test_retry_expiring_during_claim_never_calls_provider(
    journal_client, fake_clock, monkeypatch
):
    article_id, path, failed = await prepare(journal_client, fake_clock)
    assert (
        await control(journal_client, path, failed, "AUTHORIZE_RETRY", authorization_seconds=1)
    ).status_code == 200
    remote = AsyncMock(side_effect=install_interpreter(monkeypatch))
    install_http(monkeypatch, remote)
    append = AuditService.append_in_session

    async def slow_claim(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_PRODUCTION_STARTED":
            fake_clock.advance(timedelta(seconds=2))
        return await append(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", slow_claim)
    with pytest.raises(ValueError, match="not authorized"):
        await producer(fake_clock).produce(article_id)
    remote.assert_not_called()
    current = (await journal_client.get(path)).json()
    assert current["state"] == "RETRY_EXPIRED" and current["attempts"] == 1


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_browser_authorization_reaches_producer(
    journal_client, fake_clock, monkeypatch, live_dashboard, credentials
):
    article_id, path, failed = await prepare(journal_client, fake_clock)
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "frontend/tests/news-recovery-browser.mjs"),
        live_dashboard,
        env={
            **os.environ,
            "ATS_TEST_OWNER_PASSWORD": credentials[1],
            "ATS_TEST_NEWS_GROUP": failed["group_event_id"],
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"NEWS_RECOVERY_BROWSER_VERIFIED\n"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    install_interpreter(monkeypatch)
    result = await producer(fake_clock).produce(article_id)
    assert result.status == "ADMITTED"
    view = (await journal_client.get(path)).json()
    assert view["state"] == "FINISHED" and view["attempts"] == 2
