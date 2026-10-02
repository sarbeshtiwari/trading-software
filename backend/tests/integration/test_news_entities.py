"""Synthetic mentions exercise actual ingestion, immutable evidence and authenticated reads."""

from datetime import timedelta

import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import get_clock
from app.core.enums import Exchange, InstrumentType, Segment
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.news import NewsItem
from app.news.entities import mention_matches
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_ingest import configured

__all__ = ["credentials", "journal_client"]


def test_news_entity_mapping():
    catalog = [
        {
            "id": "one",
            "symbol": "TEST",
            "exchange": "NSE",
            "name": "Example Industries",
            "isin": "IN0000000001",
        }
    ]
    text = "Example Industries NSE:TEST IN0000000001 TEST CONTEST"
    result = mention_matches(text, catalog)
    assert {item["rule"] for item in result["matches"]} == {
        "QUALIFIED_SYMBOL",
        "FULL_NAME",
        "CATALOG_ISIN",
    }
    assert all(text[item["start"] : item["end"]] == item["anchor"] for item in result["matches"])
    assert result["excluded"][0]["reason"] == "BELOW_THRESHOLD"
    assert not mention_matches("CONTEST Example IndustriesExtra", catalog)["matches"]
    catalog.append({**catalog[0], "id": "two", "exchange": "BSE"})
    ambiguous = mention_matches(text, catalog)
    assert [item["rule"] for item in ambiguous["matches"]] == ["QUALIFIED_SYMBOL"]
    assert any(item["reason"] == "AMBIGUOUS" for item in ambiguous["excluded"])
    catalog[0]["isin"] = "IT"
    assert not mention_matches("IT", catalog)["matches"]


async def test_attribution_restart_revision_and_historical_catalog(journal_client, fake_clock):
    request = await configured(journal_client)
    now = get_clock().utcnow()
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="entity-test",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQUITY,
                trading_symbol="TEST",
                name="Example Industries",
                is_active=False,
                created_at=now,
                updated_at=now,
            )
        )
    request["article"]["title"] = "Synthetic Example Industries announcement"
    response = await journal_client.post("/api/v1/news/sources/fixture/articles", json=request)
    assert response.status_code == 200, response.text
    article_id = response.json()["article_id"]
    path = f"/api/v1/news/articles/{article_id}/entities"
    first = await journal_client.get(path)
    assert first.status_code == 200, first.text
    assert first.json()["attribution"]["matches"][0]["instrument_ids"] == ["entity-test"]
    assert first.json()["attribution"]["meaning"] == "MENTION_ONLY_NOT_TRADABILITY"
    assert (await journal_client.post(path)).json() == first.json()
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await journal_client.get(path)).json() == first.json()
    fake_clock.advance(timedelta(seconds=5))
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "entity-test")
        instrument.name = "Corrected Company"
        instrument.updated_at = get_clock().utcnow()
    revised = await journal_client.post(path)
    assert revised.status_code == 200, revised.text
    assert revised.json()["attribution"]["matches"] == []
    assert revised.json()["event_id"] != first.json()["event_id"]
    assert (
        await journal_client.get(path, params={"as_of": now.isoformat()})
    ).json() == first.json()
    assert (
        await journal_client.get(path, params={"as_of": (now - timedelta(seconds=1)).isoformat()})
    ).status_code == 404
    async with db_session.session_scope() as session:
        article = await session.get(NewsItem, article_id)
        assert article.entities == [] and not article.is_actionable
        article.body = "Tampered"
    assert (await journal_client.get(path)).status_code == 409
    journal_client.headers.pop("Authorization")
    assert (await journal_client.post(path)).status_code == 401


async def test_entity_audit_failure_rolls_back_import(journal_client, monkeypatch):
    request = await configured(journal_client)
    original = AuditService.append_in_session

    async def fail(self, session, identity, details, **kwargs):
        if identity.event_type == "NEWS_ENTITY_ATTRIBUTION":
            raise ValueError("Synthetic audit failure")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    response = await journal_client.post("/api/v1/news/sources/fixture/articles", json=request)
    assert response.status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 0


async def test_future_catalog_and_clock_regression(journal_client, fake_clock):
    request = await configured(journal_client)
    now = get_clock().utcnow()
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="future-entity",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQUITY,
                trading_symbol="TEST",
                created_at=now + timedelta(seconds=10),
                updated_at=now + timedelta(seconds=10),
            )
        )
    request["article"]["title"] = "Synthetic NSE:TEST announcement"
    imported = await journal_client.post("/api/v1/news/sources/fixture/articles", json=request)
    assert imported.status_code == 200, imported.text
    path = f"/api/v1/news/articles/{imported.json()['article_id']}/entities"
    first = (await journal_client.get(path)).json()
    assert first["attribution"]["matches"] == []
    fake_clock.advance(timedelta(seconds=10))
    current = await journal_client.post(path)
    assert current.status_code == 200, current.text
    assert current.json()["attribution"]["matches"][0]["instrument_ids"] == ["future-entity"]
    fake_clock.set_to(now)
    assert (await journal_client.get(path)).json() == first
    assert (await journal_client.post(path)).status_code == 409
    assert (
        await journal_client.get(path, params={"as_of": "2099-01-01T00:00:00Z"})
    ).status_code == 422
