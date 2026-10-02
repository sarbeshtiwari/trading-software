"""Broker-boundary failures retain auditable evidence without resubmission."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.errors import SafetyError
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


@pytest.mark.parametrize("fault", [
    "missing", "unavailable", "mismatch", "broker_only", "local_only", "snapshot_mismatch",
])
async def test_order_failure_evidence_and_no_duplicate_submission(
    db_engine, credentials, fake_clock, monkeypatch, fault
):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        original_orders = await engine.broker.list_orders()
        remote = original_orders[0]
        if fault in {"local_only", "snapshot_mismatch"}:
            reported = [] if fault == "local_only" else [replace(remote, quantity=remote.quantity + 1)]
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
    finally:
        await api.aclose()
