"""Owner controls and current catalog vetoes reach real PAPER entry boundaries."""

import asyncio
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.data_origin import DataOrigin
from app.core.enums import ExitReason, OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.risk.instrument_blocks import InstrumentEntryError, require_instrument_entries
from tests.integration.test_auth import credentials
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials", "live_dashboard"]
PATH = "/api/v1/risk/instrument-blocks/ins-test"


def change(blocked=True, expected=None):
    return {
        "blocked": blocked,
        "expected_event_id": expected,
        "reason": "Owner reviews instrument-specific PAPER entry eligibility",
        "confirmation": "BLOCK PAPER INSTRUMENT" if blocked else "UNBLOCK PAPER INSTRUMENT",
    }


async def test_manual_block_is_audited_durable_and_cannot_be_bypassed_by_false_flag(
    db_engine,
    credentials,
    fake_clock,
):
    engine, proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        assert not context.market.manually_blocked
        blocked = await client.put(PATH, json=change())
        assert blocked.status_code == 200, blocked.text
        view = blocked.json()
        assert view["manually_blocked"] and view["scope"] == "ALL_PAPER_DATA_ORIGINS"
        rejected = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert rejected.code == "MANUALLY_BLOCKED"
        with pytest.raises(InstrumentEntryError, match="MANUALLY_BLOCKED"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        await db_session.dispose_engine()
        db_session.init_engine()
        assert (await client.get(PATH)).json()["event_id"] == view["event_id"]
        async with db_session.session_scope() as session:
            for origin in DataOrigin:
                with pytest.raises(InstrumentEntryError):
                    await require_instrument_entries(
                        session, context.market.model_copy(update={"data_origin": origin})
                    )
            audit = await session.get(AuditEvent, view["event_id"])
            assert audit.actor == "owner" and audit.instrument_id == "ins-test"
            negative = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.chain_id == rejected.candidate_id)
            )
            assert negative.signal["event_id"] == view["event_id"]
        assert (await client.put(PATH, json=change(False))).status_code == 409
        assert (await client.put(PATH, json=change(False, view["event_id"]))).status_code == 200
        assert not (await client.get(PATH)).json()["manually_blocked"]
        await engine.submit(proposal)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()


async def test_manual_block_after_preflight_prevents_dispatch_and_duplicate_submission(
    db_engine,
    credentials,
    fake_clock,
    monkeypatch,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        original = engine._dispatch_entry_policy

        async def inhibit_after_preflight(identifier, market):
            await original(identifier, market)
            response = await client.put(PATH, json=change())
            assert response.status_code == 200, response.text

        monkeypatch.setattr(engine, "_dispatch_entry_policy", inhibit_after_preflight)
        with pytest.raises(InstrumentEntryError):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.status == OrderStatus.CREATED
            identifier = order.id
        assert await engine.submit(proposal) == identifier
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "field,code", [("is_restricted", "INSTRUMENT_RESTRICTED"), ("is_active", "INSTRUMENT_INACTIVE")]
)
async def test_catalog_change_after_approval_blocks_entry_but_not_protective_exit(
    db_engine,
    credentials,
    fake_clock,
    field,
    code,
):
    engine, proposal, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, "ins-test")
            setattr(instrument, field, field == "is_restricted")
        with pytest.raises(InstrumentEntryError, match=code):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, "ins-test")
            setattr(instrument, field, field != "is_restricted")
        await engine.submit(proposal)
        blocked = await client.put(PATH, json=change())
        assert blocked.status_code == 200, blocked.text
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, "ins-test")
            setattr(instrument, field, field == "is_restricted")
            position = await session.scalar(sa.select(Position))
            position_id = position.id
        restored = PaperExecution(
            engine.source,
            settings=engine.settings,
            clock=fake_clock,
            fill_config=engine.fill_config,
        )
        await restored.recover()
        market.update(bid=Decimal("101"), ask=Decimal("101.05"))
        await restored.exit(position_id, ExitReason.EMERGENCY)
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position_id)).net_quantity == 0
        assert (
            await client.put(PATH, json=change(False, blocked.json()["event_id"]))
        ).status_code == 200
        async with db_session.session_scope() as session:
            with pytest.raises(InstrumentEntryError, match=code):
                await require_instrument_entries(session, context.market)
    finally:
        await client.aclose()


async def test_instrument_controls_require_authentication_confirmation_and_known_instrument(
    db_engine,
    credentials,
    fake_clock,
):
    _engine, _proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        token = client.headers.pop("Authorization")
        assert (await client.put(PATH, json=change())).status_code == 401
        client.headers["Authorization"] = token
        assert (await client.put(PATH, json={**change(), "confirmation": ""})).status_code == 409
        assert (await client.put(PATH, json={**change(), "blocked": "true"})).status_code == 422
        assert (
            await client.put(PATH.replace("ins-test", "missing"), json=change())
        ).status_code == 409
        assert (await client.get("/api/v1/risk/instrument-blocks")).json() == []
    finally:
        await client.aclose()


@pytest.mark.skipif(os.environ.get("ATS_TEST_BROWSER") != "1", reason="set ATS_TEST_BROWSER=1")
async def test_real_browser_manages_audited_instrument_controls(
    db_engine,
    credentials,
    fake_clock,
    live_dashboard,
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/tests/instrument-blocks-browser.mjs"),
            live_dashboard,
            env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout == b"INSTRUMENT_CONTROLS_BROWSER_VERIFIED\n"
        async with db_session.session_scope() as session:
            events = (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.event_type == "MANUAL_INSTRUMENT_BLOCK",
                    )
                    .order_by(AuditEvent.sequence)
                )
            ).all()
            assert [event.result["change"]["blocked"] for event in events] == [True, False]
            assert all(event.actor == "owner" for event in events)
        await engine.submit(proposal)
        assert len(await engine.broker.list_orders()) == 1
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await client.aclose()
