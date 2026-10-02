"""Utilisation reads actual OMS risk evidence, not a browser-supplied account."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.security.auth import OwnerAuth
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def test_actual_paper_account_utilisation_and_staleness(db_engine, credentials, fake_clock):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        await engine.monitor_once()
        response = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "AVAILABLE", body
        metrics = {item["name"]: item for item in body["metrics"]}
        assert Decimal(metrics["gross_exposure"]["used"]) == Decimal("25000")
        assert Decimal(metrics["open_and_pending_positions"]["used"]) == 1
        async with db_session.session_scope() as session:
            evidence = await session.get(AuditEvent, body["evidence_id"])
            assert evidence.actor == "risk_safety"
            account = evidence.data_used["portfolio"]
            equity = Decimal(account["equity"])
        assert Decimal(metrics["daily_loss"]["limit"]) == min(equity, Decimal(100000)) * Decimal(
            ".02"
        )
        assert Decimal(metrics["gross_exposure"]["limit"]) == min(equity, Decimal(100000)) * 3
        missing = await client.get("/api/v1/risk/utilisation", params={"origin": "LIVE"})
        assert missing.json()["status"] == "ACCOUNT_EVIDENCE_UNAVAILABLE"
        assert missing.json()["metrics"] == []
        original_time = fake_clock.now()
        fake_clock.advance(timedelta(seconds=-1))
        access, _ = await OwnerAuth().login("owner", credentials[1])
        client.headers["Authorization"] = f"Bearer {access}"
        future = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert future.json()["status"] == "ACCOUNT_EVIDENCE_INVALID"
        assert future.json()["metrics"] == []
        fake_clock.set_to(original_time)
        fake_clock.advance(timedelta(seconds=61))
        access, _ = await OwnerAuth().login("owner", credentials[1])
        client.headers["Authorization"] = f"Bearer {access}"
        stale = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert stale.json()["status"] == "ACCOUNT_EVIDENCE_STALE"
        assert stale.json()["metrics"] == []
    finally:
        await client.aclose()


async def test_configuration_change_requires_new_risk_observation(
    db_engine, credentials, fake_clock
):
    engine, proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        response = await client.post(
            "/api/v1/risk/configuration",
            json={
                "limits": context.limits.model_copy(update={"version": 2}).model_dump(mode="json"),
                "expected_version": 1,
                "reason": "Owner changed the test-only risk configuration",
            },
        )
        assert response.status_code == 200, response.text
        response = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert response.json()["status"] == "RISK_CONFIGURATION_CHANGED"
        assert response.json()["configuration_version"] == 2
        assert response.json()["metrics"] == []
    finally:
        await client.aclose()


async def test_unreconciled_and_tampered_account_are_not_metrics(
    db_engine, credentials, fake_clock
):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            system = await session.get(SystemState, SINGLETON_ID)
            system.open_discrepancies = 1
        response = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert response.json()["status"] == "ACCOUNT_UNRECONCILED"
        assert response.json()["metrics"] == []
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.id == response.json()["evidence_id"])
            )
            event.data_used = {"portfolio": {"equity": "999999999"}}
        response = await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        assert response.json()["status"] == "ACCOUNT_EVIDENCE_INTEGRITY_FAILURE"
        assert response.json()["metrics"] == []
        client.headers.pop("Authorization")
        assert (
            await client.get("/api/v1/risk/utilisation", params={"origin": "SYNTHETIC"})
        ).status_code == 401
    finally:
        await client.aclose()
