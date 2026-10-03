"""Owner review is audited and never repairs accounting or releases trading gates."""

import pytest
import sqlalchemy as sa

from app.core.enums import PositionSide
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.db.models.trading import Position
from app.execution.orphan_review import position_snapshot
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def adopted_fixture(credentials, fake_clock):
    engine, proposal, _market, api, _context = await setup_execution(credentials, fake_clock)
    await engine.submit(proposal)
    async with db_session.session_scope() as session:
        await session.delete(await session.scalar(sa.select(Position)))
    with pytest.raises(SafetyError, match="ORPHAN_POSITION"):
        await engine.recover()
    return engine, api


async def test_owner_acknowledgment_is_idempotent_and_not_authorization(
    db_engine, credentials, fake_clock
):
    engine, api = await adopted_fixture(credentials, fake_clock)
    try:
        response = await api.get("/api/v1/reconciliation/orphans")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert not item["acknowledged"]
        identifier = item["position_id"]
        async with db_session.session_scope() as session:
            before = position_snapshot(await session.get(Position, identifier))
        body = {"reason": "Owner reviewed observed orphan evidence", "expected_head": item["head_hash"],
                "confirmation": "ACKNOWLEDGE PAPER ORPHAN"}
        endpoint = f"/api/v1/reconciliation/orphans/{identifier}/acknowledge"
        token = api.headers.pop("Authorization")
        assert (await api.get("/api/v1/reconciliation/orphans")).status_code == 401
        assert (await api.post(endpoint, json=body)).status_code == 401
        api.headers["Authorization"] = token
        assert (await api.post(endpoint, json={**body, "reason": " " * 10})).status_code == 422
        assert (await api.post(endpoint, json={**body, "confirmation": "wrong"})).status_code == 422
        assert (await api.post(endpoint, json={**body, "expected_head": "0" * 64})).status_code == 409
        result = await api.post(endpoint, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["acknowledged_by"] == "owner"
        assert result.json()["acknowledged_at"]
        assert "ENTRIES_BLOCKED" in result.json()["blockers"]
        assert (await api.post(endpoint, json=body)).json() == result.json()
        assert (await api.get("/api/v1/reconciliation/orphans")).json()["items"][0]["acknowledged"]
        async with db_session.session_scope() as session:
            assert position_snapshot(await session.get(Position, identifier)) == before
            assert (await session.get(SystemState, SINGLETON_ID)).open_discrepancies == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_ACKNOWLEDGED"
            )) == 1
        assert not get_trading_gate().new_entries_allowed and not engine.ready
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await api.aclose()


@pytest.mark.parametrize("fault", ["quantity", "side", "history", "audit"])
async def test_changed_orphan_projection_cannot_be_acknowledged(
    db_engine, credentials, fake_clock, fault
):
    _engine, api = await adopted_fixture(credentials, fake_clock)
    try:
        item = (await api.get("/api/v1/reconciliation/orphans")).json()["items"][0]
        async with db_session.session_scope() as session:
            position = await session.get(Position, item["position_id"])
            if fault == "quantity":
                position.net_quantity += 1
            elif fault == "side":
                position.side = PositionSide.SHORT
            elif fault == "history":
                position.realised_pnl = 0
            else:
                await session.execute(sa.update(AuditEvent).where(
                    AuditEvent.chain_id == position.id
                ).values(actor="fixture_tampering"))
        assert (await api.get("/api/v1/reconciliation/orphans")).status_code == 409
        result = await api.post(f"/api/v1/reconciliation/orphans/{item['position_id']}/acknowledge", json={
            "expected_head": item["head_hash"], "reason": "Owner reviewed original evidence",
            "confirmation": "ACKNOWLEDGE PAPER ORPHAN",
        })
        assert result.status_code == 409
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_ORPHAN_ACKNOWLEDGED"
            )) == 0
    finally:
        await api.aclose()
