"""Server-held calendar context is advisory evidence, never authority to place an order."""

import json
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.db import session as db_session
from app.db.models.llm import LLMCall
from app.llm.service import review_signal
from app.modes import TradingMode
from app.risk.event_controls import EventControlState
from tests.integration.test_auth import credentials
from tests.integration.test_event_controls import PATH, publication
from tests.integration.test_llm_service import install_http
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.llm_fixture import receipt
from tests.unit.test_llm import inputs, response, settings

__all__ = ["credentials"]


async def test_persisted_calendar_receipt_keeps_original_context_and_current_veto(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, _approved, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        published = await client.put(PATH, json=publication(fake_clock, delay=1))
        assert published.status_code == 200
        reference = await receipt(context, fake_clock, monkeypatch)
        async with db_session.session_scope() as session:
            call = await session.get(LLMCall, reference.call_id)
            original = call.request_context
            archived = original["inputs"]["event_control"]
            assert archived["event_id"] == published.json()["event_id"]
            assert archived["status"] == "CLEAR"
        fake_clock.advance(timedelta(seconds=1))
        await db_session.dispose_engine()
        db_session.init_engine()
        result = await DecisionPipeline(validator()).process(reference, context)
        assert result.code == "EVENT_BLACKOUT" and result.proposal_id is None
        async with db_session.session_scope() as session:
            call = await session.get(LLMCall, reference.call_id)
            assert call.request_context == original
            assert call.proposal_id is None
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_caller_cannot_supply_calendar_status_to_remote_review(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    _engine, _approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    captured = []

    def remote(request):
        body = json.loads(request.content)
        captured.append(
            json.loads(
                body["messages"][0]["content"]
                .removeprefix("<UNTRUSTED_DATA>")
                .removesuffix("</UNTRUSTED_DATA>")
            )
        )
        return httpx.Response(200, json=response())

    try:
        published = await client.put(PATH, json=publication(fake_clock))
        assert published.status_code == 200
        spoof = EventControlState(
            instrument_id="ins-test",
            origin="SYNTHETIC",
            as_of=fake_clock.utcnow(),
            status="CLEAR",
            event_id="caller-invented-head",
        )
        install_http(monkeypatch, remote)
        await review_signal(
            inputs().model_copy(update={"event_control": spoof}),
            settings(),
            clock=fake_clock,
            mode=TradingMode.PAPER,
            correlation_id="calendar-fixture",
        )
        assert len(captured) == 1
        assert captured[0]["event_control"]["status"] == "BLACKOUT"
        assert captured[0]["event_control"]["event_id"] == published.json()["event_id"]
    finally:
        await client.aclose()


async def test_calendar_clock_failure_does_not_call_remote_or_hide_as_clear(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    _engine, _approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        fake_clock.advance(timedelta(seconds=1))
        assert (await client.put(PATH, json=publication(fake_clock))).status_code == 200
        fake_clock.advance(timedelta(seconds=-1))
        install_http(monkeypatch, lambda request: pytest.fail("Unsafe calendar must block HTTP"))
        with pytest.raises(ValueError, match="clock regression"):
            await review_signal(
                inputs(),
                settings(),
                clock=fake_clock,
                mode=TradingMode.PAPER,
                correlation_id="calendar-fixture",
            )
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(LLMCall)
                    .where(LLMCall.provider == "claude")
                )
                == 0
            )
    finally:
        await client.aclose()
