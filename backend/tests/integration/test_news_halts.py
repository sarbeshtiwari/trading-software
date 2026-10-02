"""Real admitted news and PAPER holdings drive durable, instrument-only entry vetoes."""

import asyncio
import os
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.enums import ExitReason
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Position
from app.execution.freshness import require_entry_sources
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate
from app.news.reactions import cycle
from app.risk.news_halts import NewsHaltError, require_no_news_halt, state_at
from tests.integration.test_auth import credentials
from tests.integration.test_news_research_admission import interpret, observe
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials", "live_dashboard"]


async def enable(client, *, max_age_seconds=3600):
    response = await client.put(
        "/api/v1/risk/news-policy",
        json={
            "policy": {
                "enabled": True,
                "accept_uncalibrated_labels": True,
                "minimum_confidence": "0.8",
                "max_age_seconds": max_age_seconds,
            },
            "expected_event_id": None,
            "reason": "Owner enables conservative fixture news inhibition",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def news(client, monkeypatch, *, magnitude="HIGH", confidence="0.9"):
    article, _version = await observe(client, "halt-primary", primary=True)
    request = await interpret(
        client,
        article,
        monkeypatch,
        direction="NEGATIVE",
        magnitude=magnitude,
        confidence=confidence,
    )
    response = await client.post(f"/api/v1/news/articles/{article}/research", json=request)
    assert response.status_code == 200, response.text
    return response.json()["source_id"]


async def test_news_blocks_new_entries_and_recovers_without_blocking_exits(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        second = await quant_decision(
            DecisionPipeline(validator()),
            payload(),
            context.model_copy(update={"cycle_id": "after-news-exit-cycle"}),
        )
        assert second.code == "RISK_APPROVED"
        await engine.submit(proposal)
        await enable(client)
        before = fake_clock.utcnow()
        fake_clock.advance(timedelta(seconds=1))
        admission = await news(client, monkeypatch)
        halts = (await client.get("/api/v1/risk/news-halts")).json()
        assert len(halts) == 1 and halts[0]["blocked"]
        assert halts[0]["causes"][0]["admission_id"] == admission
        assert not context.market.news_halt
        result = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert result.code == "NEWS_HALT" and result.proposal_id is None
        async with db_session.session_scope() as session:
            assert not (
                await state_at(session, "ins-test", context.market.data_origin, as_of=before)
            ).blocked
            await require_no_news_halt(
                session, context.market.model_copy(update={"instrument_id": "unrelated"})
            )
            position = await session.scalar(sa.select(Position))
            position_id = position.id
        await cycle()
        await db_session.dispose_engine()
        db_session.init_engine()
        restarted = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restarted.recover()
        async with db_session.session_scope() as session:
            with pytest.raises(NewsHaltError):
                await require_no_news_halt(session, context.market)
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "NEWS_INSTRUMENT_HALT")
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
        market.update(bid=Decimal("101"), ask=Decimal("101.05"))
        await restarted.exit(position_id, ExitReason.EMERGENCY)
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position_id)).net_quantity == 0
        with pytest.raises(NewsHaltError):
            await restarted.submit(second.proposal_id)
        body = {
            "origin": context.market.data_origin.value,
            "expected_event_id": halts[0]["event_id"],
            "reason": "Owner reviewed source and remaining entry restrictions",
            "confirmation": "REVIEWED NEWS HALT",
        }
        response = await client.post("/api/v1/risk/news-halts/ins-test/release", json=body)
        assert response.status_code == 200, response.text
        assert not response.json()["blocked"] and response.json()["acknowledged"] == [admission]
        assert (
            await client.post("/api/v1/risk/news-halts/ins-test/release", json=body)
        ).status_code == 409
        await cycle()
        assert not (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "magnitude,confidence,held,enabled",
    [
        ("UNKNOWN", "0.9", True, True),
        ("HIGH", "0.7", True, True),
        ("HIGH", "0.9", False, True),
        ("HIGH", "0.9", True, False),
    ],
)
async def test_news_reaction_requires_held_instrument_policy_and_severity(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
    magnitude,
    confidence,
    held,
    enabled,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        if held:
            await engine.submit(proposal)
        if enabled:
            await enable(client)
        await news(client, monkeypatch, magnitude=magnitude, confidence=confidence)
        await cycle()
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
    finally:
        await client.aclose()


async def test_halt_preflight_release_dedupe_and_policy_disable(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        second = await quant_decision(
            DecisionPipeline(validator()),
            payload(),
            context.model_copy(update={"cycle_id": "second-news-halt-cycle"}),
        )
        assert second.code == "RISK_APPROVED"
        await engine.submit(proposal)
        policy = await enable(client)
        gate_before = get_trading_gate().state.to_dict()
        await news(client, monkeypatch)
        async with db_session.session_scope() as session:
            approved = await session.get(Proposal, second.proposal_id)
        with pytest.raises(NewsHaltError):
            await require_entry_sources(approved, context.limits, fake_clock.utcnow())
        assert len(await engine.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            with pytest.raises(NewsHaltError):
                await engine.safety.require_entries_in_session(
                    session, context.market, context.limits
                )
        assert get_trading_gate().state.to_dict() == gate_before
        halted = (await client.get("/api/v1/risk/news-halts")).json()[0]
        disabled = await client.put(
            "/api/v1/risk/news-policy",
            json={
                "policy": {**policy["policy"], "enabled": False},
                "expected_event_id": policy["event_id"],
                "reason": "Disable future news reactions without clearing existing halt",
            },
        )
        assert disabled.status_code == 200
        assert (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
        body = {
            "origin": "SYNTHETIC",
            "expected_event_id": halted["event_id"],
            "reason": "Reviewed source quotes and held position exposure",
            "confirmation": "REVIEWED NEWS HALT",
        }
        authorization = client.headers.pop("Authorization")
        assert (
            await client.post("/api/v1/risk/news-halts/ins-test/release", json=body)
        ).status_code == 401
        client.headers["Authorization"] = authorization
        assert (
            await client.post(
                "/api/v1/risk/news-halts/ins-test/release", json={**body, "confirmation": ""}
            )
        ).status_code == 422
        assert (
            await client.post("/api/v1/risk/news-halts/ins-test/release", json=body)
        ).status_code == 200
        policy["event_id"] = disabled.json()["event_id"]
        assert (
            await client.put(
                "/api/v1/risk/news-policy",
                json={
                    "policy": policy["policy"],
                    "expected_event_id": policy["event_id"],
                    "reason": "Reenable policy without replaying acknowledged evidence",
                },
            )
        ).status_code == 200
        await cycle()
        assert not (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
    finally:
        await client.aclose()


async def test_news_before_position_is_evaluated_by_periodic_worker_service(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await enable(client)
        await news(client, monkeypatch)
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
        await engine.submit(proposal)
        await cycle()
        assert (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
    finally:
        await client.aclose()


async def test_stale_admission_does_not_create_retroactive_halt(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await news(client, monkeypatch)
        fake_clock.advance(timedelta(seconds=11))
        await enable(client, max_age_seconds=10)
        await cycle()
        assert (await client.get("/api/v1/risk/news-halts")).json() == []
    finally:
        await client.aclose()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_actual_dashboard_displays_and_releases_persisted_news_halt(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
    live_dashboard,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        await engine.submit(proposal)
        await enable(client)
        await news(client, monkeypatch)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/news-halts-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"NEWS_HALT_BROWSER_VERIFIED\n"
        await cycle()
        assert not (await client.get("/api/v1/risk/news-halts")).json()[0]["blocked"]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
