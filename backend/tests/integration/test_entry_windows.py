"""Direct application entry paths enforce session policy without a running worker."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.calendar import TradingCalendar
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position
from app.risk.audit import RiskAudit
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


@pytest.mark.parametrize("stage", ["pipeline", "preflight", "dispatch"])
async def test_blackout_blocks_direct_entries(
    db_engine, credentials, fake_clock, monkeypatch, stage
):
    engine, proposal_id, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        if stage == "dispatch":
            original = engine._dispatch

            async def changed_policy(identifier, **kwargs):
                credentials[0].entry_blackout_open_minutes = 90
                return await original(identifier, **kwargs)

            monkeypatch.setattr(engine, "_dispatch", changed_policy)
        else:
            credentials[0].entry_blackout_open_minutes = 90
        if stage == "pipeline":
            result = await quant_decision(DecisionPipeline(validator()), payload(), context)
            assert result.code == "ENTRY_WINDOW_BLOCKED"
            assert result.approved_quantity == 0
        else:
            with pytest.raises(SafetyError, match="ENTRY_WINDOW_BLOCKED"):
                await engine.submit(proposal_id)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            decisions = list((await session.scalars(sa.select(RiskDecision))).all())
        for decision in decisions:
            assert (await RiskAudit().replay(decision.id)).approved == decision.approved
        workspace = (await client.get("/api/v1/workspace")).json()
        if stage == "preflight":
            assert workspace["preflights"][0]["entry_policy"]["window"]["phase"] == "MARKET_OPEN"
    finally:
        await client.aclose()


async def test_incomplete_calendar_blocks_entry_but_blackout_allows_exit(
    db_engine, credentials, fake_clock
):
    engine, proposal_id, market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        engine.calendar = TradingCalendar()
        with pytest.raises(SafetyError, match="ENTRY_WINDOW_BLOCKED"):
            await engine.submit(proposal_id)
        assert await engine.broker.list_orders() == []
        engine.calendar = TradingCalendar(complete_years=[2026], source="isolated")
        await engine.submit(proposal_id)
        credentials[0].entry_blackout_open_minutes = 90
        market.update(bid=Decimal(104), ask=Decimal("104.05"))
        await engine.monitor_once()
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            journal = await session.scalar(
                sa.select(JournalEntry).where(JournalEntry.kind == "TRADE")
            )
            assert position.net_quantity == 0
            assert journal is not None
        assert len(await engine.broker.list_orders()) == 2
    finally:
        await client.aclose()
