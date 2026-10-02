"""Synthetic multi-publisher observations through actual grouping and history APIs."""

from datetime import timedelta

import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.news.dedupe import canonical_url, same_story
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_controls import body

__all__ = ["credentials", "journal_client"]


def test_news_dedupe_similarity():
    assert canonical_url("https://Example.test:443/report#section") == "https://example.test/report"
    assert canonical_url("https://example.test/Report") != canonical_url(
        "https://example.test/report"
    )
    first = {"title": "Synthetic corporate announcement", "body": "Synthetic exact article text"}
    assert same_story(first, {**first, "body": "SYNTHETIC exact ARTICLE text."})
    assert not same_story(first, {**first, "body": "Synthetic opposite statement"})
    assert not same_story(first, {**first, "body": ""})
    assert same_story(
        {**first, "url": "https://example.test:443/report"},
        {**first, "url": "https://example.test/report#update", "body": "Corrected report"},
    )
    long_body = " ".join(f"fixture{index}" for index in range(40))
    assert same_story({**first, "body": long_body}, {**first, "body": long_body + " additional"})
    assert not same_story(
        {**first, "body": long_body}, {**first, "body": " ".join(reversed(long_body.split()))}
    )


async def imported(client, slug, *, publisher=None, article_body=None, origin="SYNTHETIC"):
    configuration = body()
    configuration["configuration"].update(
        publisher_id=publisher or slug,
        endpoint=f"https://{slug}.example.test/feed",
        is_enabled=True,
    )
    source = await client.put(f"/api/v1/news/sources/{slug}", json=configuration)
    assert source.status_code == 200, source.text
    response = await client.post(
        f"/api/v1/news/sources/{slug}/articles",
        json={
            "expected_event_id": source.json()["event_id"],
            "article": {
                "url": f"https://{slug}.example.test/article",
                "title": "Synthetic corporate announcement",
                "body": article_body or "Synthetic identical source text for grouping only.",
                "published_at": (get_clock().utcnow() - timedelta(minutes=5)).isoformat(),
                "data_origin": origin,
            },
        },
    )
    return response


async def test_three_sources_one_versioned_story(journal_client, fake_clock):
    first = await imported(journal_client, "first")
    assert first.status_code == 200, first.text
    path = f"/api/v1/news/articles/{first.json()['article_id']}/story"
    initial = await journal_client.get(path)
    assert initial.status_code == 200, initial.text
    cutoff = get_clock().utcnow()
    for slug in ("second", "third"):
        fake_clock.advance(timedelta(seconds=1))
        response = await imported(journal_client, slug)
        assert response.status_code == 200, response.text
        read = await journal_client.get(
            f"/api/v1/news/articles/{response.json()['article_id']}/story"
        )
        assert read.json()["group_id"] == initial.json()["group_id"]
    current = await journal_client.get(path)
    assert len(current.json()["story"]["members"]) == 3
    assert {member["publisher_id"] for member in current.json()["story"]["members"]} == {
        "first",
        "second",
        "third",
    }
    assert current.json()["story"]["verification_status"] == "UNVERIFIED"
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await journal_client.get(path)).json() == current.json()
    assert (
        await journal_client.get(path, params={"as_of": cutoff.isoformat()})
    ).json() == initial.json()
    async with db_session.session_scope() as session:
        rows = list((await session.scalars(sa.select(NewsItem))).all())
        assert len(rows) == 3 and all(not row.is_actionable for row in rows)
        rows[0].body = "Tampered raw source"
    assert (await journal_client.get(path)).status_code == 409


async def test_different_text_and_origin_do_not_merge(journal_client):
    first = await imported(journal_client, "first")
    second = await imported(journal_client, "second", article_body="Synthetic conflicting report")
    third = await imported(journal_client, "third", origin="HISTORICAL")
    groups = []
    for response in (first, second, third):
        assert response.status_code == 200, response.text
        read = await journal_client.get(
            f"/api/v1/news/articles/{response.json()['article_id']}/story"
        )
        assert read.status_code == 200, read.text
        groups.append(read.json()["group_id"])
    assert len(set(groups)) == 3


async def test_story_audit_failure_rolls_back_article(journal_client, monkeypatch):
    original = AuditService.append_in_session

    async def fail(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_STORY_MEMBERSHIP":
            raise ValueError("Synthetic audit failure")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    response = await imported(journal_client, "failed")
    assert response.status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 0


async def test_complete_link_prevents_similarity_bridge(journal_client, fake_clock):
    words = [f"fixture{index}" for index in range(25)]
    texts = [" ".join(["changed", *words[1:]]), " ".join(words), " ".join([*words[:-1], "changed"])]
    groups = []
    for index, text in enumerate(texts):
        response = await imported(journal_client, f"source{index}", article_body=text)
        assert response.status_code == 200, response.text
        result = await journal_client.get(
            f"/api/v1/news/articles/{response.json()['article_id']}/story"
        )
        groups.append(result.json()["group_id"])
        fake_clock.advance(timedelta(seconds=1))
    assert groups[0] == groups[1] and groups[2] != groups[0]


async def test_ambiguous_groups_are_not_arbitrarily_merged(journal_client):
    words = [f"fixture{index}" for index in range(25)]
    groups = []
    for index, words_for_article in enumerate(
        (
            ["changed", *words[1:]],
            [*words[:-1], "changed"],
            words,
        )
    ):
        response = await imported(
            journal_client, f"source{index}", article_body=" ".join(words_for_article)
        )
        assert response.status_code == 200, response.text
        result = await journal_client.get(
            f"/api/v1/news/articles/{response.json()['article_id']}/story"
        )
        groups.append(result.json()["group_id"])
    assert len(set(groups)) == 3
    assert result.json()["story"]["ambiguous_group_match"]


async def test_grouping_clock_regression_and_future_reads(journal_client, fake_clock):
    now = get_clock().utcnow()
    response = await imported(journal_client, "first")
    assert response.status_code == 200, response.text
    path = f"/api/v1/news/articles/{response.json()['article_id']}/story"
    assert (
        await journal_client.get(path, params={"as_of": (now - timedelta(seconds=1)).isoformat()})
    ).status_code == 404
    assert (
        await journal_client.get(path, params={"as_of": "2099-01-01T00:00:00Z"})
    ).status_code == 422
    fake_clock.advance(timedelta(seconds=10))
    second = await imported(journal_client, "second")
    assert second.status_code == 200, second.text
    fake_clock.set_to(now)
    rejected = await imported(journal_client, "clock-regressed")
    assert rejected.status_code == 409
    journal_client.headers.pop("Authorization")
    assert (await journal_client.get(path)).status_code == 401
