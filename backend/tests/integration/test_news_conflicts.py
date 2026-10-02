"""Synthetic opposing source statements through actual ingestion and interpretation gates."""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.llm import LLMCall
from app.db.models.news import NewsItem
from app.news.conflicts import detect, statements
from app.news.dedupe import StoryMember
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_dedupe import imported

__all__ = ["credentials", "journal_client"]


async def test_news_contradiction(journal_client, fake_clock, monkeypatch):
    statement = "Example company will approve the proposed transaction."
    negation = "Example company will not approve the proposed transaction."
    background = " " + " ".join(f"fixture{index}" for index in range(80)) + "."
    first = await imported(journal_client, "first", article_body=statement + background)
    assert first.status_code == 200, first.text
    article_id = first.json()["article_id"]
    path = f"/api/v1/news/articles/{article_id}/conflicts"
    prior = await journal_client.get(path)
    assert prior.status_code == 200, prior.text
    assert prior.json()["assessment"]["status"] == "UNASSESSED"
    cutoff = get_clock().utcnow()
    fake_clock.advance(timedelta(seconds=1))
    second = await imported(journal_client, "second", article_body=negation + background)
    assert second.status_code == 200, second.text
    current = await journal_client.get(path)
    assert current.status_code == 200, current.text
    assessment = current.json()["assessment"]
    assert assessment["status"] == "CONFLICTING" and not assessment["actionable"]
    assert len(assessment["oppositions"]) == 1
    pair = assessment["oppositions"][0]
    assert pair["asserted"]["quote"] == statement
    assert pair["negated"]["quote"] == negation
    assert (statement + background)[
        pair["asserted"]["start"] : pair["asserted"]["end"]
    ] == statement
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await journal_client.get(path)).json() == current.json()
    assert (
        await journal_client.get(path, params={"as_of": cutoff.isoformat()})
    ).json() == prior.json()

    def forbidden(*args, **kwargs):
        pytest.fail("Known conflict must stand down before paid provider selection")

    monkeypatch.setattr("app.news.interpret.provider_for", forbidden)
    request = {"expected_event_id": assessment["group_event_id"]}
    rejected = await journal_client.post(
        f"/api/v1/news/articles/{article_id}/interpretation", json=request
    )
    assert rejected.status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(LLMCall)) == 0
        rows = list((await session.scalars(sa.select(NewsItem))).all())
        assert all(not row.is_actionable for row in rows)
        rows[0].body = "Tampered source"
    assert (await journal_client.get(path)).status_code == 409
    journal_client.headers.pop("Authorization")
    assert (await journal_client.get(path)).status_code == 401


def test_hedges_double_negation_and_same_source_are_not_resolved(fake_clock):
    member = StoryMember(
        article_id="fixture",
        article_audit_id="audit",
        source_slug="source",
        source_event_id="source-audit",
        publisher_id="publisher",
        canonical_url="https://example.test/article",
        title="Synthetic",
        published_at=get_clock().utcnow(),
        observed_at=get_clock().utcnow(),
    )
    for text in (
        "Example company may not approve the proposed transaction.",
        "Example company will not only approve the proposed transaction.",
        "Example company will not never approve the proposed transaction.",
    ):
        assert statements(text, member=member, field="body") == []
    positive = statements(
        "Example company will approve the proposed transaction.", member=member, field="body"
    )
    negative = statements(
        "Example company will not approve the proposed transaction.", member=member, field="body"
    )
    assert detect([*positive, *negative]) == []
    multiline = "  Example company will approve the proposed transaction\nOther text."
    extracted = statements(multiline, member=member, field="body")
    assert len(extracted) == 1
    anchor = extracted[0][1]
    assert anchor.quote == "Example company will approve the proposed transaction"
    assert multiline[anchor.start : anchor.end] == anchor.quote


async def test_failed_conflict_audit_rolls_back_ingestion(journal_client, monkeypatch):
    original = AuditService.append_in_session

    async def fail(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_CONFLICT_ASSESSMENT":
            raise ValueError("Synthetic conflict audit failure")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    assert (await imported(journal_client, "fixture")).status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 0
