"""Actual PAPER discrepancies remain blocked until authenticated fresh-state review."""

import asyncio
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.calendar import TradingCalendar
from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.system import SINGLETON_ID, Discrepancy, SystemState
from app.db.models.trading import Position
from app.execution import discrepancies
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate
from app.trading.worker import PaperWorker, _runtime
from tests.integration.test_paper_browser import ROOT, live_dashboard
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials", "live_dashboard"]


async def test_durable_review_restart_safe_exit_and_authenticated_resolution(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, live_dashboard
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    worker = None
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            identifier, quantity = position.id, position.net_quantity
            position.net_quantity += 1
        for _attempt in range(2):
            with pytest.raises(SafetyError, match="RECONCILE"):
                await engine._reconcile()
        current = (await api.get("/api/v1/reconciliation")).json()
        assert len(current["items"]) == 1
        item = current["items"][0]
        assert list(item["record"]["delta"]["quantities"].values()) == [1]
        async with db_session.session_scope() as session:
            assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 1
            (await session.get(Position, identifier)).net_quantity = quantity
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        worker = PaperWorker(restarted, calendar=TradingCalendar(complete_years=[2026]),
                             lock_path=tmp_path / "review.lock")
        await worker.start(schedule=False)
        monkeypatch.setattr(_runtime, "worker", worker)
        endpoint = f"/api/v1/reconciliation/{item['record']['id']}/resolve"
        body = {"expected_head": item["head_hash"], "reason": "Owner reviewed restored fixture accounting",
                "confirmation": "RESOLVE PAPER DISCREPANCY"}
        async with db_session.session_scope() as session:
            (await session.get(Position, identifier)).net_quantity = quantity + 2
        refused = await api.post(endpoint, json=body)
        assert refused.status_code == 409
        async with db_session.session_scope() as session:
            assert not (await session.get(Discrepancy, item["record"]["id"])).resolved
            (await session.get(Position, identifier)).net_quantity = quantity
        await restarted._reconcile(allow_review_pending=True)
        changed = await api.post(endpoint, json=body)
        assert changed.status_code == 409
        assert "CHANGED_REVIEW_AGAIN" in changed.text
        item = (await api.get("/api/v1/reconciliation")).json()["items"][0]
        body["expected_head"] = item["head_hash"]
        assert "REVIEW_REQUIRED" in get_trading_gate().reason()
        with pytest.raises(SafetyError, match="RECONCILIATION"):
            await restarted._reconcile()
        await restarted.exit(identifier, ExitReason.EMERGENCY)
        assert (await api.get("/api/v1/reconciliation")).json()["items"][0]["record"]["resolved"] is False
        endpoint = f"/api/v1/reconciliation/{item['record']['id']}/resolve"
        body = {"expected_head": item["head_hash"], "reason": "Owner reviewed restored fixture accounting",
                "confirmation": "RESOLVE PAPER DISCREPANCY"}
        assert (await api.post(endpoint, json={**body, "confirmation": "wrong"})).status_code == 422
        assert (await api.post(endpoint, json={**body, "expected_head": "0" * 64})).status_code == 409
        get_trading_gate().block("independent", "OWNER_OTHER_GATE")
        if os.environ.get("ATS_TEST_BROWSER") == "1":
            process = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "frontend/tests/reconciliation-browser.mjs"),
                live_dashboard, item["record"]["id"],
                env={**os.environ, "ATS_TEST_OWNER_PASSWORD": credentials[1]},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
                assert process.returncode == 0, stderr.decode(errors="replace")
                assert stdout == b"RECONCILIATION_REVIEW_BROWSER_VERIFIED\n"
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
        result = await api.post(endpoint, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["record"]["resolved_by"] == "owner"
        assert result.json()["record"]["resolved"]
        assert (await api.post(endpoint, json=body)).status_code == 200
        assert "OWNER_OTHER_GATE" in get_trading_gate().reason()
        assert "paper_reconciliation" not in get_trading_gate().reason()
        assert (await api.get("/api/v1/reconciliation")).json()["items"] == []
        async with db_session.session_scope() as session:
            assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 0
            closed = await session.get(Position, identifier)
            assert closed.net_quantity == 0
            record = await session.get(Discrepancy, item["record"]["id"])
            assert len(await discrepancies.verify(session, record)) == 3
        assert len(await restarted.broker.list_orders()) == 2
    finally:
        if worker is not None and worker.running:
            await worker.stop()
        await api.aclose()


async def test_discrepancy_persistence_failure_does_not_claim_recovery(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            (await session.scalar(sa.select(Position))).average_price += 1

        async def fail(*args, **kwargs):
            raise RuntimeError("fixture evidence failure")

        monkeypatch.setattr(discrepancies, "append", fail)
        with pytest.raises(RuntimeError, match="evidence failure"):
            await engine._reconcile()
        assert not get_trading_gate().new_entries_allowed
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Discrepancy)) == 0
            assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 0
    finally:
        await api.aclose()


async def test_changed_discrepancy_projection_fails_closed(
    db_engine, credentials, fake_clock
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            (await session.scalar(sa.select(Position))).average_price += 1
        with pytest.raises(SafetyError, match="RECONCILE"):
            await engine._reconcile()
        current = await api.get("/api/v1/reconciliation")
        assert current.status_code == 200
        item = current.json()["items"][0]
        assert {Decimal(value) for value in item["record"]["delta"]["average_prices"].values()} == {Decimal("1")}
        async with db_session.session_scope() as session:
            row = await session.get(Discrepancy, item["record"]["id"])
            row.delta = {"quantities": {}}
        assert (await api.get("/api/v1/reconciliation")).status_code == 409
        with pytest.raises(SafetyError, match="EVIDENCE_INVALID"):
            await engine._reconcile()
        assert not get_trading_gate().new_entries_allowed
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await api.aclose()
