"""Actual authenticated source policy updates, audit rollback and stale-write refusal."""

import asyncio

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.db import session as db_session
from app.db.models.news import NewsSource
from app.modes import TradingMode
from app.news.sources import chain_id
from tests.integration.test_journal_api import credentials, journal_client

__all__ = ["credentials", "journal_client"]


def body(**changes):
    return {
        "configuration": {
            "name": "Isolated configured source",
            "publisher_id": "fixture-publisher",
            "tier": 2,
            "kind": "rss",
            "endpoint": "https://publisher.example.test/feed",
            "weight": "1",
            "is_enabled": False,
        },
        "expected_event_id": None,
        "reason": "Owner configures isolated test source",
        **changes,
    }


async def test_source_control_auth_audit_restart_and_disable(journal_client):
    client = journal_client
    assert (await client.get("/api/v1/news/sources")).json() == {"sources": [], "has_more": False}
    created = await client.put("/api/v1/news/sources/fixture", json=body())
    assert created.status_code == 200, created.text
    first = created.json()
    assert first["integrity"] == "AUDITED" and first["acquisition_status"] == "SUPPORTED_ADAPTER"
    await db_session.dispose_engine()
    db_session.init_engine()
    read = (await client.get("/api/v1/news/sources/fixture")).json()
    assert read["event_id"] == first["event_id"] and read["integrity"] == "AUDITED"
    changed = body(expected_event_id=first["event_id"])
    changed["configuration"]["is_enabled"] = True
    updated = await client.put("/api/v1/news/sources/fixture", json=changed)
    assert updated.status_code == 200
    assert (await client.put("/api/v1/news/sources/fixture", json=changed)).status_code == 409
    changed["expected_event_id"] = updated.json()["event_id"]
    changed["configuration"]["is_enabled"] = False
    assert (await client.put("/api/v1/news/sources/fixture", json=changed)).status_code == 200
    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(NewsSource))
        assert not row.is_enabled and row.publisher_id == "fixture-publisher"
    history = await AuditService().chain(chain_id("fixture"))
    assert len(history) == 3 and all(event.actor == "owner" for event in history)
    assert history[-1].result["reason"] == changed["reason"]
    assert await AuditService().verify(chain_id("fixture"))
    client.headers.pop("Authorization")
    assert (await client.get("/api/v1/news/sources")).status_code == 401
    assert (await client.put("/api/v1/news/sources/fixture", json=body())).status_code == 401


async def test_source_concurrency_and_audit_failure(journal_client, monkeypatch):
    client = journal_client
    first = (await client.put("/api/v1/news/sources/fixture", json=body())).json()
    change = body(expected_event_id=first["event_id"])
    change["configuration"]["weight"] = "0.5"
    results = await asyncio.gather(
        *(client.put("/api/v1/news/sources/fixture", json=change) for _attempt in range(2))
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    original = AuditService.append_in_session

    async def failure(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_SOURCE_CONFIGURATION":
            raise ValueError("isolated failed audit write")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", failure)
    assert (await client.put("/api/v1/news/sources/another", json=body())).status_code == 409
    assert (await client.get("/api/v1/news/sources/another")).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"tier": 1},
        {"publisher_id": ""},
        {"endpoint": "http://publisher.example.test/feed"},
        {"endpoint": "https://user:private-fixture-password@publisher.example.test/feed"},
        {"endpoint": "https://publisher.example.test/feed?token=private-fixture-password"},
        {"endpoint": "https://publisher.example.test:private-fixture-password/feed"},
    ],
)
async def test_invalid_source_cannot_be_published(journal_client, changes):
    request = body()
    request["configuration"].update(changes)
    result = await journal_client.put("/api/v1/news/sources/fixture", json=request)
    assert result.status_code == 422 and "private-fixture-password" not in result.text
    assert (await journal_client.get("/api/v1/news/sources")).json()["sources"] == []


async def test_source_mutation_detected_and_not_overwritten(journal_client):
    first = (await journal_client.put("/api/v1/news/sources/fixture", json=body())).json()
    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(NewsSource))
        row.publisher_id = "tampered"
    assert (await journal_client.get("/api/v1/news/sources/fixture")).json()[
        "integrity"
    ] == "INVALID"
    response = await journal_client.put(
        "/api/v1/news/sources/fixture", json=body(expected_event_id=first["event_id"])
    )
    assert response.status_code == 409


async def test_source_controls_remain_paper_only(journal_client, credentials):
    credentials[0].trading_mode = TradingMode.SUPERVISED
    assert (
        await journal_client.put("/api/v1/news/sources/fixture", json=body())
    ).status_code == 423
