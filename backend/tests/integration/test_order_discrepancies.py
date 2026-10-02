"""Broker-boundary failures retain auditable evidence without resubmission."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.trading import Position, Trade
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def accounting_state():
    async with db_session.session_scope() as session:
        positions = (await session.execute(sa.select(
            Position.id, Position.net_quantity, Position.average_price,
            Position.realised_pnl, Position.total_charges,
        ).order_by(Position.id))).all()
        fills = (await session.execute(sa.select(
            Trade.id, Trade.quantity, Trade.price,
        ).order_by(Trade.id))).all()
        return positions, fills


@pytest.mark.parametrize("fault", [
    "missing", "unavailable", "mismatch", "broker_only", "local_only", "snapshot_mismatch",
    "terminal_status", "fill_regression", "trades_unavailable", "snapshot_status",
])
async def test_order_failure_evidence_and_no_duplicate_submission(
    db_engine, credentials, fake_clock, monkeypatch, fault
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        before = await accounting_state()
        original_orders = await engine.broker.list_orders()
        remote = original_orders[0]
        if fault in {"local_only", "snapshot_mismatch", "snapshot_status"}:
            reported = [] if fault == "local_only" else [replace(remote, quantity=remote.quantity + 1)]
            if fault == "snapshot_status":
                reported = [replace(remote, status=OrderStatus.OPEN)]
            monkeypatch.setattr(engine.broker, "list_orders", AsyncMock(return_value=reported))
            with pytest.raises(SafetyError, match="ORDER_RECONCILE"):
                await engine._reconcile(allow_review_pending=True)
            expected = "PAPER_ORDER_SNAPSHOT_MISMATCH"
        elif fault == "broker_only":
            untracked = replace(remote, reference_id="fixture_untracked", broker_order_id="fixture_other")
            monkeypatch.setattr(engine.broker, "list_orders", AsyncMock(return_value=[remote, untracked]))
            for _attempt in range(2):
                with pytest.raises(SafetyError, match="UNTRACKED"):
                    await engine._reconcile(allow_review_pending=True)
            expected = "UNTRACKED_PAPER_BROKER_ORDER"
        else:
            lookup = AsyncMock(return_value=None)
            expected = "REFERENCE_NOT_OBSERVED"
            if fault == "unavailable":
                lookup = AsyncMock(side_effect=ConnectionError("private fixture connection detail"))
                expected = "BROKER_LOOKUP_UNAVAILABLE"
            elif fault == "mismatch":
                lookup = AsyncMock(return_value=replace(remote, quantity=remote.quantity + 1))
                expected = "BROKER_ORDER_OR_FILL_MISMATCH"
            elif fault == "terminal_status":
                lookup = AsyncMock(return_value=replace(remote, status=OrderStatus.OPEN))
                expected = "TERMINAL_ORDER_STATUS_MISMATCH"
            elif fault == "fill_regression":
                lookup = AsyncMock(return_value=replace(
                    remote, filled_quantity=remote.filled_quantity - 1
                ))
                expected = "FILL_REGRESSION"
            elif fault == "trades_unavailable":
                lookup = AsyncMock(return_value=remote)
                monkeypatch.setattr(engine.broker, "list_trades", AsyncMock(
                    side_effect=ConnectionError("private fixture trade failure")
                ))
                expected = "BROKER_TRADES_UNAVAILABLE"
            monkeypatch.setattr(engine.broker, "get_order_by_reference", lookup)
            with pytest.raises((SafetyError, ConnectionError)):
                await engine.sync(identifier)
        response = await api.get("/api/v1/reconciliation")
        assert response.status_code == 200
        assert "private fixture" not in response.text
        rows = response.json()["items"]
        assert len(rows) == 1
        record = rows[0]["record"]
        assert record["delta"] == {"order_failure": expected, "automatic_resubmission": False}
        assert record["local_state"]["orders"][0]["id"] == identifier
        if fault in {"missing", "unavailable", "local_only"}:
            assert record["broker_state"]["orders"] == []
        else:
            assert record["broker_state"]["orders"][0]["broker_order_id"]
        assert not get_trading_gate().new_entries_allowed
        assert await engine.submit(proposal) == identifier
        assert len(engine.broker._orders) == 1
        assert await accounting_state() == before
    finally:
        await api.aclose()
