"""Discover real PAPER broker fixture state without inventing missing local history."""

import asyncio
import os

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason, PositionState
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Position, Trade
from app.execution import orphans
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials", "live_dashboard"]


async def test_orphan_adoption_restart_api_and_unavailable_accounting(
    db_engine, credentials, fake_clock, live_dashboard
):
    engine, proposal, _market, api, context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            original = await session.scalar(sa.select(Position))
            quantity, price = original.net_quantity, original.average_price
            await session.delete(original)
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        for _attempt in range(2):
            with pytest.raises(SafetyError, match="ORPHAN_POSITION"):
                await restarted.recover()
        async with db_session.session_scope() as session:
            rows = list(await session.scalars(sa.select(Position)))
            assert len(rows) == 1
            adopted = rows[0]
            assert adopted.state == PositionState.ADOPTED and adopted.adopted_from_broker
            assert (adopted.net_quantity, adopted.average_price) == (quantity, price)
            assert adopted.realised_pnl is adopted.unrealised_pnl is adopted.total_charges is None
            assert adopted.net_pnl is None
            assert adopted.proposal_id is adopted.strategy_id is adopted.opened_at is None
            assert not adopted.is_protected and adopted.stop_loss_price is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_ADOPTED"
            )) == 1
            with pytest.raises(ValueError, match="adopted position"):
                await restarted.safety.require_entries_in_session(session, context.market, context.limits)
        with pytest.raises(SafetyError, match="ADOPTED_POSITION"):
            await restarted.portfolio_state(context.strategy.id, context.market.data_origin)
        with pytest.raises(SafetyError, match="ADOPTED_POSITION"):
            await restarted.exit(adopted.id, ExitReason.EMERGENCY)
        result = (await api.get("/api/v1/workspace")).json()
        assert result["account"] is None
        assert result["account_status"] == "UNAVAILABLE_ADOPTED_POSITION_ACCOUNTING"
        assert result["positions"][0]["state"] == "ADOPTED"
        assert result["positions"][0]["realised_pnl"] is None
        assert result["positions"][0]["accounting_status"] == "UNAVAILABLE_HISTORY_NOT_RECONSTRUCTED"
        assert not get_trading_gate().new_entries_allowed and not restarted.ready
        assert len(await restarted.broker.list_orders()) == 1
        discrepancies = (await api.get("/api/v1/reconciliation")).json()["items"]
        assert discrepancies[0]["record"]["broker_state"]["orphan_positions"][0]["quantity"] == quantity
        if os.environ.get("ATS_TEST_BROWSER") == "1":
            process = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "frontend/tests/orphan-browser.mjs"), live_dashboard,
                env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                assert process.returncode == 0, stderr.decode(errors="replace")
                assert stdout == b"ORPHAN_VISIBILITY_BROWSER_VERIFIED\n"
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
    finally:
        await api.aclose()


async def test_orphan_notification_failure_rolls_back_adoption(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            await session.delete(await session.scalar(sa.select(Position)))

        async def fail(*args, **kwargs):
            raise RuntimeError("fixture notification persistence failure")

        monkeypatch.setattr(orphans, "enqueue", fail)
        with pytest.raises(RuntimeError, match="notification persistence"):
            await engine.recover()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_ADOPTED"
            )) == 0
        assert not engine.ready and not get_trading_gate().new_entries_allowed
    finally:
        await api.aclose()
