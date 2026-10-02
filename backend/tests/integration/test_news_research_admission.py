"""Real imported observations become bounded source-policy evidence, never fake live facts."""

import asyncio
import json
import os
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsItem
from app.news.research import corroborating_support
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_llm_service import install_http
from tests.integration.test_news_availability import news_context
from tests.integration.test_news_controls import body
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_llm import response, settings
from tests.unit.test_proposal import payload

__all__ = ["credentials", "journal_client", "live_dashboard"]
QUOTE = "NSE:TEST reported a synthetic corporate announcement."


def test_independence_dimensions_must_hold_for_the_same_pair():
    supports = [
        {
            "source": {"tier": 2, "publisher_id": "first", "url": "https://one.test/story"},
            "body_fingerprint": "body-one",
        },
        {
            "source": {"tier": 2, "publisher_id": "second", "url": "https://one.test/story"},
            "body_fingerprint": "body-two",
        },
        {
            "source": {"tier": 2, "publisher_id": "first", "url": "https://two.test/story"},
            "body_fingerprint": "body-two",
        },
    ]
    assert corroborating_support(supports) == []
    supports[-1]["source"]["publisher_id"] = "third"
    assert corroborating_support(supports) == [supports[0], supports[2]]


async def observe(
    client,
    slug,
    *,
    primary=False,
    publisher=None,
    suffix="",
    host=None,
    quote=QUOTE,
    title="Synthetic announcement",
    weight="1",
    origin="SYNTHETIC",
):
    host = host or slug
    request = body()
    request["configuration"].update(
        publisher_id=publisher or slug,
        endpoint=f"https://{host}.example.test/feed",
        kind="filings" if primary else "rss",
        tier=1 if primary else 2,
        is_enabled=True,
        weight=weight,
    )
    configured = await client.put(f"/api/v1/news/sources/{slug}", json=request)
    assert configured.status_code == 200, configured.text
    text = quote + " " + " ".join(f"fixture{index}" for index in range(80)) + suffix
    imported = await client.post(
        f"/api/v1/news/sources/{slug}/articles",
        json={
            "expected_event_id": configured.json()["event_id"],
            "article": {
                "url": f"https://{host}.example.test/article",
                "title": title,
                "body": text,
                "published_at": get_clock().utcnow().isoformat(),
                "data_origin": origin,
            },
        },
    )
    assert imported.status_code == 200, imported.text
    return imported.json()["article_id"], configured.json()["event_id"]


def install_interpreter(
    monkeypatch, *, quote=QUOTE, direction="UNKNOWN", confidence="0.5", magnitude="UNKNOWN"
):
    monkeypatch.setattr("app.news.interpret.get_settings", settings)

    def handler(request):
        envelope = json.loads(request.content)
        inputs = json.loads(
            envelope["messages"][0]["content"]
            .removeprefix("<UNTRUSTED_DATA>")
            .removesuffix("</UNTRUSTED_DATA>")
        )
        value = {
            "event_type": "UNKNOWN",
            "instrument_ids": inputs["instrument_ids"],
            "direction": direction,
            "magnitude": magnitude,
            "confidence": confidence,
            "time_horizon": "UNKNOWN",
            "summary": quote,
            "claims": [
                {
                    "article_id": article["article_id"],
                    "field": "body",
                    "start": 0,
                    "end": len(quote),
                    "quote": quote,
                }
                for article in inputs["articles"]
            ],
        }
        return httpx.Response(200, json=response(raw=json.dumps(value)))

    install_http(monkeypatch, handler)
    return handler


async def interpret(
    client,
    article_id,
    monkeypatch,
    *,
    quote=QUOTE,
    direction="UNKNOWN",
    confidence="0.5",
    magnitude="UNKNOWN",
):
    install_interpreter(
        monkeypatch, quote=quote, direction=direction, confidence=confidence, magnitude=magnitude
    )
    story = (await client.get(f"/api/v1/news/articles/{article_id}/story")).json()
    result = await client.post(
        f"/api/v1/news/articles/{article_id}/interpretation",
        json={"expected_event_id": story["event_id"]},
    )
    assert result.status_code == 200, result.text
    return {"instrument_id": "ins-test", "interpretation_event_id": result.json()["event_id"]}


async def test_primary_admission_reaches_shared_pipeline_and_survives_restart(
    journal_client, fake_clock, monkeypatch
):
    context, _strategy = await news_context()
    article_id, version = await observe(journal_client, "primary", primary=True)
    request = await interpret(journal_client, article_id, monkeypatch)
    path = f"/api/v1/news/articles/{article_id}/research"
    assert (
        await journal_client.post(path, json={**request, "instrument_id": "unmapped"})
    ).status_code == 409
    admitted = await journal_client.post(path, json=request)
    assert admitted.status_code == 200, admitted.text
    evidence_id = admitted.json()["source_id"]
    assert admitted.json()["status"] == "SOURCE_POLICY_ADMITTED"
    assert admitted.json()["snapshot"]["body"] == QUOTE
    assert admitted.json()["snapshot"]["sentiment_score"] is None
    assert (await journal_client.post(path, json=request)).json() == admitted.json()
    decision = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": evidence_id}]),
        context,
    )
    assert decision.code == "RISK_APPROVED", decision
    async with db_session.session_scope() as session:
        article = await session.get(NewsItem, article_id)
        assert not article.is_actionable and article.entities == []
    await db_session.dispose_engine()
    db_session.init_engine()
    endpoint = "/api/v1/news/research/ins-test"
    assert (await journal_client.get(endpoint, params={"origin": "SYNTHETIC"})).json()["articles"][
        0
    ]["source_id"] == evidence_id
    assert (await journal_client.get(endpoint, params={"origin": "LIVE"})).json()[
        "status"
    ] == "UNAVAILABLE"
    prior = get_clock().utcnow()
    assert (
        await journal_client.get(
            endpoint,
            params={"origin": "SYNTHETIC", "as_of": (prior - timedelta(seconds=1)).isoformat()},
        )
    ).json()["status"] == "UNAVAILABLE"
    fake_clock.advance(timedelta(seconds=1))
    change = body(expected_event_id=version)
    change["configuration"].update(
        publisher_id="primary", endpoint="https://primary.example.test/feed", kind="filings", tier=1
    )
    assert (
        await journal_client.put("/api/v1/news/sources/primary", json=change)
    ).status_code == 200
    assert (await journal_client.get(endpoint, params={"origin": "SYNTHETIC"})).json()[
        "status"
    ] == "UNAVAILABLE"
    assert (
        await journal_client.get(
            endpoint, params={"origin": "SYNTHETIC", "as_of": prior.isoformat()}
        )
    ).json()["status"] == "AVAILABLE"


@pytest.mark.parametrize("case", ["single", "aliases", "syndicated", "same_host", "independent"])
async def test_secondary_sources_need_independent_supported_text(journal_client, monkeypatch, case):
    await news_context()
    article_id, _version = await observe(journal_client, "first", suffix=" first")
    if case != "single":
        await observe(
            journal_client,
            "second",
            publisher="first" if case == "aliases" else "second",
            suffix=" first" if case == "syndicated" else " second",
            host="first" if case == "same_host" else None,
        )
    request = await interpret(journal_client, article_id, monkeypatch)
    result = await journal_client.post(f"/api/v1/news/articles/{article_id}/research", json=request)
    assert result.status_code == (200 if case == "independent" else 409), result.text
    if case == "independent":
        assert result.json()["snapshot"]["publisher_count"] == 2
        assert not result.json()["snapshot"]["standalone_trigger_allowed"]


async def test_admission_audit_failure_leaves_no_admitted_evidence(journal_client, monkeypatch):
    await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    request = await interpret(journal_client, article_id, monkeypatch)
    original = AuditService.append_in_session

    async def fail(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_RESEARCH_ADMITTED":
            raise ValueError("Synthetic admission audit failure")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    assert (
        await journal_client.post(f"/api/v1/news/articles/{article_id}/research", json=request)
    ).status_code == 409
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "NEWS_RESEARCH_ADMITTED")
            )
            == 0
        )
    result = await journal_client.get(
        "/api/v1/news/research/ins-test", params={"origin": "SYNTHETIC"}
    )
    assert result.json()["status"] == "UNAVAILABLE"


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_browser_admits_prepared_research(
    journal_client, live_dashboard, credentials, monkeypatch
):
    context, _strategy = await news_context()
    article_id, _version = await observe(journal_client, "primary", primary=True)
    await interpret(journal_client, article_id, monkeypatch, direction="POSITIVE", confidence="1")
    observed = (await journal_client.get(f"/api/v1/news/articles/{article_id}")).json()["article"]
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "frontend/tests/news-admission-browser.mjs"),
        live_dashboard,
        env={
            **os.environ,
            "ATS_TEST_OWNER_PASSWORD": credentials[1],
            "ATS_TEST_NEWS_ARTICLE": json.dumps(observed),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"NEWS_ADMISSION_BROWSER_VERIFIED\n"
        research = (
            await journal_client.get(
                "/api/v1/news/research/ins-test", params={"origin": "SYNTHETIC"}
            )
        ).json()
        result = await quant_decision(
            DecisionPipeline(validator()),
            payload(evidence=[{"kind": "NEWS", "source_id": research["articles"][0]["source_id"]}]),
            context,
        )
        assert result.code == "RISK_APPROVED"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
