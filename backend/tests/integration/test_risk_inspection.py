"""Actual pipeline/preflight receipts and untrusted historical evidence refusal."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.risk.audit import RiskAudit
from app.security.auth import OwnerAuth
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.unit.test_risk import limits, market, portfolio, proposal

__all__ = ["credentials"]


async def test_pipeline_and_preflight_decisions_are_bound_and_replayable(
    db_engine, credentials, fake_clock
):
    engine, identifier, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(identifier)
        response = await client.get("/api/v1/risk/decisions")
        assert response.status_code == 200
        rows = response.json()["items"]
        assert len(rows) == 2
        assert {row["is_preflight"] for row in rows} == {False, True}
        for row in rows:
            response = await client.get(f"/api/v1/risk/decisions/{row['id']}")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "AUDIT_BOUND_REPLAY_VERIFIED", body
            assert body["historical_only"] and body["decision"]["approved"]
            per_trade = next(
                rule for rule in body["decision"]["rules"] if rule["rule"] == "per_trade"
            )
            assert Decimal(per_trade["inputs"]["risk"]) == 500
            assert Decimal(per_trade["inputs"]["limit"]) == 500
        fake_clock.advance(timedelta(seconds=-1))
        access, _ = await OwnerAuth().login("owner", credentials[1])
        client.headers["Authorization"] = f"Bearer {access}"
        assert (await client.get(f"/api/v1/risk/decisions/{rows[0]['id']}")).status_code == 404
        assert (await client.get("/api/v1/risk/decisions")).json()["items"] == []
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", ["risk_amount", "state_snapshot"])
async def test_tampering_and_rejected_decision_visibility(
    db_engine, credentials, fake_clock, change
):
    _engine, _identifier, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier, decision = await RiskAudit().evaluate_and_record(
            proposal(quantity=251), portfolio(), market(), limits()
        )
        response = await client.get(f"/api/v1/risk/decisions/{identifier}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "AUDIT_BOUND_REPLAY_VERIFIED"
        assert not response.json()["decision"]["approved"]
        assert response.json()["decision"]["binding_rule"] == decision.binding_rule
        async with db_session.session_scope() as session:
            row = await session.get(RiskDecision, identifier)
            if change == "risk_amount":
                row.risk_amount = Decimal(1)
            else:
                row.state_snapshot = {**row.state_snapshot, "extra": "tampered"}
        response = await client.get(f"/api/v1/risk/decisions/{identifier}")
        assert response.json()["status"] == "CORRUPT"
        assert response.json()["decision"] is None
        client.headers.pop("Authorization")
        assert (await client.get(f"/api/v1/risk/decisions/{identifier}")).status_code == 401
    finally:
        await client.aclose()


async def test_receipt_failure_rolls_back_decision(db_engine, monkeypatch):
    monkeypatch.setattr(
        "app.risk.audit.bind_decision", AsyncMock(side_effect=RuntimeError("receipt unavailable"))
    )
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        await RiskAudit().evaluate_and_record(proposal(), portfolio(), market(), limits())
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(RiskDecision)) == 0


async def test_legacy_decision_is_not_retroactively_certified(db_engine, credentials, fake_clock):
    _engine, _identifier, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            session.add(
                RiskDecision(
                    id="legacy-decision",
                    proposal_id="legacy-proposal",
                    approved=False,
                    mode="PAPER",
                    evaluated_at=fake_clock.utcnow(),
                )
            )
        response = await client.get("/api/v1/risk/decisions/legacy-decision")
        assert response.status_code == 200
        assert response.json()["status"] == "LEGACY_UNSEALED"
        assert response.json()["decision"] is None
    finally:
        await client.aclose()


async def test_preflight_receipt_failure_never_submits(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, identifier, _market, client, _context = await setup_execution(credentials, fake_clock)
    monkeypatch.setattr(
        "app.execution.paper.bind_decision",
        AsyncMock(side_effect=RuntimeError("receipt unavailable")),
    )
    try:
        with pytest.raises(RuntimeError, match="receipt unavailable"):
            await engine.submit(identifier)
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(RiskDecision)
                    .where(RiskDecision.is_preflight.is_(True))
                )
                == 0
            )
    finally:
        await client.aclose()
