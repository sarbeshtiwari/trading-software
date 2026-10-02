"""Published calendar evidence reaches shared PAPER decisions and final entry checks."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.enums import ExitReason, OrderStatus
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.fundamental_versions import CorporateCalendarVersion
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.risk.event_controls import EventControlError, state_at
from tests.integration.test_auth import credentials
from tests.integration.test_corporate_calendar import calendar_evidence
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]
PATH = "/api/v1/risk/event-controls/ins-test"


async def test_current_calendar_does_not_fall_back_after_clock_regression(
    db_engine, credentials, fake_clock
):
    _engine, _approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        initial = fake_clock.utcnow()
        fake_clock.advance(timedelta(seconds=1))
        assert (await client.put(PATH, json=publication(fake_clock))).status_code == 200
        fake_clock.advance(timedelta(seconds=-1))
        async with db_session.session_scope() as session:
            with pytest.raises(ValueError, match="clock regression"):
                await state_at(session, "ins-test", "SYNTHETIC")
            assert (
                await state_at(session, "ins-test", "SYNTHETIC", as_of=initial)
            ).status == "UNCONFIGURED"
    finally:
        await client.aclose()


def publication(clock, *, delay=0, expected=None, strategies=()):
    now = clock.utcnow()
    return {
        "origin": "SYNTHETIC",
        "enabled": True,
        "max_age_seconds": 600,
        "expected_event_id": expected,
        "restricted_strategies": strategies,
        "reason": "Owner publishes isolated fixture event evidence",
        "confirmation": "PUBLISH PAPER EVENT CONTROL",
        "calendars": [
            {
                "source": "fixture-calendar",
                "known_at": now.isoformat(),
                "coverage_start": (now - timedelta(seconds=60)).isoformat(),
                "coverage_end": (now + timedelta(seconds=600)).isoformat(),
                "events": [
                    {
                        "id": "fixture-policy",
                        "source": "fixture-calendar",
                        "known_at": now.isoformat(),
                        "start": (now + timedelta(seconds=delay)).isoformat(),
                        "end": (now + timedelta(seconds=delay + 120)).isoformat(),
                        "high_impact": True,
                        "underlyings": ["TEST"],
                    }
                ],
            }
        ],
    }


async def test_calendar_overrides_false_flags_and_caller_declared_optional_calendar(
    db_engine,
    credentials,
    fake_clock,
):
    engine, approved, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        assert not context.market.event_blackout and not context.strategy.requires_event_calendar
        response = await client.put(PATH, json=publication(fake_clock))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "BLACKOUT"
        result = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert result.code == "EVENT_BLACKOUT" and result.proposal_id is None
        with pytest.raises(EventControlError, match="EVENT_BLACKOUT"):
            await engine.submit(approved)
        assert await engine.broker.list_orders() == []
        before = fake_clock.utcnow() - timedelta(seconds=1)
        async with db_session.session_scope() as session:
            assert (
                await state_at(session, "ins-test", "SYNTHETIC", as_of=before)
            ).status == "UNCONFIGURED"
            assert (await state_at(session, "ins-test", "LIVE")).status == "UNCONFIGURED"
    finally:
        await client.aclose()


async def test_clear_calendar_snapshot_survives_execution_restart_and_blackout_exit(
    db_engine,
    credentials,
    fake_clock,
):
    engine, _old, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        published = await client.put(PATH, json=publication(fake_clock, delay=30))
        assert published.status_code == 200, published.text
        result = await quant_decision(
            DecisionPipeline(validator()),
            payload(),
            context.model_copy(update={"cycle_id": "calendar-clear-cycle"}),
        )
        assert result.code == "RISK_APPROVED"
        order_id = await engine.submit(result.proposal_id)
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, result.proposal_id)
            order = await session.get(Order, order_id)
            assert (
                proposal.context_snapshot["event_control"]["event_id"]
                == published.json()["event_id"]
            )
            assert order.request_payload["event_control"]["status"] == "CLEAR"
            position_id = (await session.scalar(sa.select(Position))).id
        await db_session.dispose_engine()
        db_session.init_engine()
        fake_clock.advance(timedelta(seconds=30))
        restored = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restored.recover()
        assert (await client.get(PATH, params={"origin": "SYNTHETIC"})).json()[
            "status"
        ] == "BLACKOUT"
        market.update(bid=Decimal("101"), ask=Decimal("101.05"), observed=fake_clock.now())
        await restored.exit(position_id, ExitReason.EMERGENCY)
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position_id)).net_quantity == 0
    finally:
        await client.aclose()


async def test_calendar_publication_between_preflight_and_dispatch_vetoes_order(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        original = engine._dispatch_entry_policy

        async def publish_late(identifier, market):
            await original(identifier, market)
            response = await client.put(PATH, json=publication(fake_clock))
            assert response.status_code == 200, response.text

        monkeypatch.setattr(engine, "_dispatch_entry_policy", publish_late)
        with pytest.raises(EventControlError):
            await engine.submit(approved)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert (await session.scalar(sa.select(Order))).status == OrderStatus.CREATED
    finally:
        await client.aclose()


async def test_corporate_store_publication_is_atomic_scoped_and_explicitly_reviewed(
    db_engine,
    credentials,
    fake_clock,
):
    engine, approved, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        corporate = calendar_evidence().model_dump(mode="json")
        body = {
            **publication(fake_clock),
            "calendars": [],
            "corporate": corporate,
            "before_seconds": 172800,
            "after_seconds": 86400,
        }
        response = await client.put(PATH, json=body)
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["status"] == "BLACKOUT"
        async with db_session.session_scope() as session:
            record = await session.get(CorporateCalendarVersion, state["corporate_record_id"])
            assert record.payload == corporate
        with pytest.raises(EventControlError):
            await engine.submit(approved)
        assert (await client.put(PATH, json=body)).status_code == 409
        body.update(expected_event_id=state["event_id"], restricted_strategies=["another-strategy"])
        changed = await client.put(PATH, json=body)
        assert changed.status_code == 200, changed.text
        assert (
            await client.get(
                PATH, params={"origin": "SYNTHETIC", "strategy_id": context.strategy.id}
            )
        ).json()["status"] == "NOT_APPLICABLE"
        await engine.submit(approved)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("condition", ["stale", "coverage"])
async def test_unknown_or_stale_configured_calendar_is_not_clear(
    db_engine,
    credentials,
    fake_clock,
    condition,
):
    engine, approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        body = publication(fake_clock, delay=30)
        if condition == "stale":
            body["max_age_seconds"] = 1
            fake_clock.advance(timedelta(seconds=2))
        else:
            body["calendars"][0]["coverage_start"] = (
                fake_clock.utcnow() + timedelta(seconds=10)
            ).isoformat()
        response = await client.put(PATH, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "UNAVAILABLE"
        with pytest.raises(EventControlError, match="EVENT_CALENDAR_UNAVAILABLE"):
            await engine.submit(approved)
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_failed_audit_rolls_back_corporate_import(
    db_engine, credentials, fake_clock, monkeypatch
):
    _engine, _approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:

        async def fail_audit(*args, **kwargs):
            raise ValueError("Injected unavailable audit store")

        monkeypatch.setattr(AuditService, "append_in_session", fail_audit)
        body = {
            **publication(fake_clock),
            "calendars": [],
            "corporate": calendar_evidence().model_dump(mode="json"),
            "before_seconds": 172800,
            "after_seconds": 86400,
        }
        assert (await client.put(PATH, json=body)).status_code == 409
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count()).select_from(CorporateCalendarVersion)
                )
                == 0
            )
            assert (await state_at(session, "ins-test", "SYNTHETIC")).status == "UNCONFIGURED"
    finally:
        await client.aclose()


async def test_future_evidence_and_unreviewed_disable_rejected(db_engine, credentials, fake_clock):
    engine, approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        future = publication(fake_clock)
        future["calendars"][0]["known_at"] = (
            fake_clock.utcnow() + timedelta(seconds=1)
        ).isoformat()
        assert (await client.put(PATH, json=future)).status_code == 409
        body = publication(fake_clock)
        response = await client.put(PATH, json=body)
        assert response.status_code == 200
        body.update(enabled=False, expected_event_id=response.json()["event_id"])
        assert (await client.put(PATH, json=body)).status_code == 409
        with pytest.raises(EventControlError):
            await engine.submit(approved)
        body["confirmation"] = "DISABLE PAPER EVENT CONTROL"
        released = await client.put(PATH, json=body)
        assert released.status_code == 200 and released.json()["status"] == "DISABLED"
        await engine.submit(approved)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()
