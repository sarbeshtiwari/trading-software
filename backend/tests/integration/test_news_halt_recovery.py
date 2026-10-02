"""Faults and concurrent review boundaries over actual admission and worker services."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order, Position
from app.news.reactions import cycle
from tests.integration.test_auth import credentials
from tests.integration.test_news_halts import enable, news
from tests.integration.test_news_research_admission import interpret, observe
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.integration.test_reference_worker import setup_worker
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


async def prepare_admission(client, monkeypatch, name):
    article, _version = await observe(client, name, primary=True)
    request = await interpret(
        client, article, monkeypatch, direction="NEGATIVE", magnitude="HIGH", confidence="0.9"
    )
    return f"/api/v1/news/articles/{article}/research", request


async def test_admission_halt_and_notification_commit_or_rollback_together(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await enable(client)
        path, request = await prepare_admission(client, monkeypatch, "atomic-halt")
        await cycle()
        assert (await client.get("/api/v1/risk/news-halts")).json() == []

        async def storage_failure(*args, **kwargs):
            raise sa.exc.OperationalError(
                "isolated notification persistence fault", {}, Exception("unavailable")
            )

        with monkeypatch.context() as failure:
            failure.setattr("app.risk.news_halts.enqueue", storage_failure)
            rejected = await client.post(path, json=request)
        assert rejected.status_code == 409, rejected.text
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.event_type.in_(
                            ["NEWS_RESEARCH_ADMITTED", "NEWS_INSTRUMENT_HALT"]
                        )
                    )
                )
                == 0
            )
            assert (await session.scalar(sa.select(Position))).net_quantity == 250
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
        committed = await client.post(path, json=request)
        assert committed.status_code == 200, committed.text
        assert (await client.post(path, json=request)).json() == committed.json()
        async with db_session.session_scope() as session:
            for event_type in ("NEWS_RESEARCH_ADMITTED", "NEWS_INSTRUMENT_HALT"):
                assert (
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(AuditEvent)
                        .where(AuditEvent.event_type == event_type)
                    )
                    == 1
                )
            notices = (
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "NOTIFICATION_REQUESTED")
                )
            ).all()
            assert (
                sum(
                    event.result["notification"]["event_type"] == "NEWS_INSTRUMENT_HALT"
                    for event in notices
                )
                == 1
            )
    finally:
        await client.aclose()


async def test_new_admission_invalidates_old_review_and_can_relatch_after_release(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await enable(client)
        first = await news(client, monkeypatch)
        initial = (await client.get("/api/v1/risk/news-halts")).json()[0]
        body = {
            "origin": "SYNTHETIC",
            "expected_event_id": initial["event_id"],
            "reason": "Review covers only the original admitted evidence",
            "confirmation": "REVIEWED NEWS HALT",
        }
        path, request = await prepare_admission(client, monkeypatch, "additional-halt")
        second = await client.post(path, json=request)
        assert second.status_code == 200, second.text
        changed = (await client.get("/api/v1/risk/news-halts")).json()[0]
        assert changed["event_id"] != initial["event_id"] and len(changed["causes"]) == 2
        assert (
            await client.post("/api/v1/risk/news-halts/ins-test/release", json=body)
        ).status_code == 409
        reviewed = {**body, "expected_event_id": changed["event_id"]}
        released = await client.post("/api/v1/risk/news-halts/ins-test/release", json=reviewed)
        assert released.status_code == 200, released.text
        assert released.json()["acknowledged"] == [first, second.json()["source_id"]]
        assert (await client.post(path, json=request)).status_code == 200
        await cycle()
        assert not (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
        third_path, third_request = await prepare_admission(
            client, monkeypatch, "new-reviewed-version"
        )
        third = await client.post(third_path, json=third_request)
        assert third.status_code == 200, third.text
        latest = (await client.get("/api/v1/risk/news-halts")).json()[0]
        assert latest["blocked"] and len(latest["causes"]) == 1
        assert latest["causes"][0]["admission_id"] == third.json()["source_id"]
    finally:
        await client.aclose()


async def test_real_worker_discovers_preexisting_news_after_fill_and_still_exits(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    monkeypatch,
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await enable(client)
        await news(client, monkeypatch)
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity > 0
            position_id = position.id
        await worker.cycle()
        assert not worker.failed, worker.detail
        halted = (await client.get("/api/v1/risk/news-halts")).json()[0]
        assert halted["blocked"] and halted["causes"][0]["positions"] == [position_id]
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            ltp=Decimal(108),
            bids=(replace(provider.get_quote.return_value.bids[0], price=Decimal(108)),),
            asks=(replace(provider.get_quote.return_value.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position_id)).net_quantity == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
        assert (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
    finally:
        await worker.stop()
        await client.aclose()


async def test_future_and_wrong_origin_news_do_not_halt_current_paper_holdings(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await enable(client)
        historical, _version = await observe(
            client, "historical-halt", primary=True, origin="HISTORICAL"
        )
        story = (await client.get(f"/api/v1/news/articles/{historical}/story")).json()
        assert (
            await client.post(
                f"/api/v1/news/articles/{historical}/interpretation",
                json={
                    "expected_event_id": story["event_id"],
                },
            )
        ).status_code == 409
        article, version = await observe(
            client, "other-origin-fixture", primary=True, origin="LIVE"
        )
        request = await interpret(
            client, article, monkeypatch, direction="NEGATIVE", magnitude="HIGH", confidence="0.9"
        )
        response = await client.post(f"/api/v1/news/articles/{article}/research", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["snapshot"]["data_origin"] == "LIVE"
        future = await client.post(
            "/api/v1/news/sources/other-origin-fixture/articles",
            json={
                "expected_event_id": version,
                "article": {
                    "url": "https://other-origin-fixture.example.test/future",
                    "title": "Future synthetic fixture",
                    "body": "NSE:TEST synthetic future quoted news.",
                    "published_at": (fake_clock.utcnow() + timedelta(days=1)).isoformat(),
                    "data_origin": "SYNTHETIC",
                },
            },
        )
        assert future.status_code == 409, future.text
        await cycle()
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
    finally:
        await client.aclose()


async def test_corrupt_halt_ledger_fails_closed_in_shared_pipeline(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await enable(client)
        await news(client, monkeypatch)
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "NEWS_INSTRUMENT_HALT")
            )
            event.result = {"cause": {"origin": "HISTORICAL", "admission_id": "tampered"}}
        rejected = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert rejected.proposal_id is None and rejected.code == "INVALID_DECISION_CONTEXT"
        assert len(await engine.broker.list_orders()) == 1
        risk = (await client.get("/api/v1/risk")).json()
        assert risk["latches"]["SYNTHETIC"]["engine_error"]
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "NEGATIVE_DECISION")
                )
                >= 1
            )
    finally:
        await client.aclose()
