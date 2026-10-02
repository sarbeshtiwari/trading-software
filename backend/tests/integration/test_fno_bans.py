"""Real source admission, durable vetoes and preserved CASH exits; F&O OMS stays disabled."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.clock import IST
from app.core.enums import Exchange, ExitReason, InstrumentType, Segment
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.trading import Position
from app.fno.restrictions import BanEntryError, require_entries, state_at
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]
PATH = "/api/v1/risk/fno-bans"


async def test_old_revision_cannot_clear_ban_and_prior_day_never_carries_forward(
    db_engine,
    credentials,
    fake_clock,
):
    _engine, _approved, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        await derivative(fake_clock)
        first = await client.put(PATH, json=admission(fake_clock))
        assert first.status_code == 200
        conflict = admission(fake_clock, head=first.json()["head_id"], symbols=())
        assert (await client.put(PATH, json=conflict)).status_code == 409
        fake_clock.advance(timedelta(days=1))
        async with db_session.session_scope() as session:
            assert (await state_at(session, "SYNTHETIC")).status == "UNAVAILABLE"
            market = context.market.model_copy(update={"instrument_id": "fno-fixture"})
            with pytest.raises(BanEntryError, match="FNO_BAN_REPORT_UNAVAILABLE"):
                await require_entries(session, market)
    finally:
        await client.aclose()


def admission(clock, *, head=None, day=None, symbols=("TEST",)):
    day = day or clock.utcnow().astimezone(IST).date()
    return {
        "origin": "SYNTHETIC",
        "known_at": clock.utcnow().isoformat(),
        "document": f"Securities in Ban For Trade Date {day.strftime('%d-%b-%Y').upper()}:\n"
        + "".join(f"{index},{symbol}\n" for index, symbol in enumerate(symbols, 1)),
        "expected_event_id": head,
        "reason": "Owner reviews isolated source-format fixture",
        "confirmation": "ADMIT PAPER NSE BAN REPORT",
    }


async def derivative(clock):
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="fno-fixture",
                exchange=Exchange.NSE,
                segment=Segment.FNO,
                instrument_type=InstrumentType.FUTURE,
                trading_symbol="TEST-FUT",
                underlying="TEST",
                expiry_date=clock.utcnow().date() + timedelta(days=7),
                lot_size=25,
                tick_size=Decimal("0.05"),
                is_active=True,
                is_restricted=False,
                created_at=clock.utcnow(),
                source="isolated fixture",
            )
        )


async def test_ban_admission_drives_shared_decision_veto_and_survives_restart(
    db_engine,
    credentials,
    fake_clock,
):
    engine, _approved, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        await derivative(fake_clock)
        context = context.model_copy(
            update={
                "market": context.market.model_copy(
                    update={"instrument_id": "fno-fixture", "ban_listed": False}
                )
            }
        )
        missing = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert missing.code == "FNO_BAN_REPORT_UNAVAILABLE"
        published = await client.put(PATH, json=admission(fake_clock))
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "AVAILABLE"
        await db_session.dispose_engine()
        db_session.init_engine()
        blocked = await quant_decision(DecisionPipeline(validator()), payload(), context)
        assert blocked.code == "FNO_BAN_LISTED" and blocked.proposal_id is None
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            with pytest.raises(BanEntryError, match="FNO_BAN_LISTED"):
                await require_entries(session, context.market)
        assert (await client.put(PATH, json=admission(fake_clock))).status_code == 409
    finally:
        await client.aclose()


async def test_next_session_report_does_not_replace_today_and_does_not_affect_cash(
    db_engine,
    credentials,
    fake_clock,
):
    engine, approved, market, client, context = await setup_execution(credentials, fake_clock)
    try:
        today = await client.put(PATH, json=admission(fake_clock))
        assert today.status_code == 200
        tomorrow = await client.put(
            PATH,
            json=admission(
                fake_clock,
                head=today.json()["head_id"],
                day=fake_clock.utcnow().astimezone(IST).date() + timedelta(days=1),
                symbols=(),
            ),
        )
        assert tomorrow.status_code == 200
        assert tomorrow.json()["event_id"] == today.json()["event_id"]
        assert tomorrow.json()["head_id"] != today.json()["head_id"]
        await engine.submit(approved)
        async with db_session.session_scope() as session:
            position_id = (await session.scalar(sa.select(Position))).id
            await require_entries(session, context.market)
        market.update(bid=Decimal("101"), ask=Decimal("101.05"))
        await engine.exit(position_id, ExitReason.EMERGENCY)
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position_id)).net_quantity == 0
            before = fake_clock.utcnow() - timedelta(seconds=1)
            assert (await state_at(session, "SYNTHETIC", as_of=before)).status == "UNAVAILABLE"
            assert (await state_at(session, "LIVE")).status == "UNAVAILABLE"
    finally:
        await client.aclose()


async def test_admission_requires_authentication_and_nonfuture_source_knowledge(
    db_engine,
    credentials,
    fake_clock,
):
    _engine, _approved, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        token = client.headers.pop("Authorization")
        assert (await client.put(PATH, json=admission(fake_clock))).status_code == 401
        client.headers["Authorization"] = token
        body = admission(fake_clock)
        body["known_at"] = (fake_clock.utcnow() + timedelta(seconds=1)).isoformat()
        assert (await client.put(PATH, json=body)).status_code == 409
        body = admission(fake_clock)
        body["document"] = ""
        assert (await client.put(PATH, json=body)).status_code == 422
        assert (await client.get(PATH, params={"origin": "SYNTHETIC"})).json()["head_id"] is None
    finally:
        await client.aclose()
