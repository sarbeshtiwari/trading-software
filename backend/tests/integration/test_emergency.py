"""Owner controls enforce durable blocking while retaining the existing safe exit path."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.enums import HealthStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.db.models.trading import Position
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import HealthCheck, get_health_registry
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]


async def test_mode_health_and_unexpected_refusals_remain_fail_closed(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, _, _, client, _ = await setup_execution(credentials, fake_clock)
    controls = EmergencyControls(fake_clock)
    try:
        await controls.activate("KILL", actor="owner", reason="Owner tests refusal evidence")
        body = {
            "action": "CLEAR",
            "reason": "Owner reviews blocked clear request",
            "confirmation": "CLEAR PAPER EMERGENCY",
        }
        with monkeypatch.context() as scoped:
            scoped.setattr(credentials[0], "trading_mode", TradingMode.SUPERVISED)
            assert (await client.post("/api/v1/emergency", json=body)).status_code == 423
        worker = SimpleNamespace(
            running=True, cycle_lock=asyncio.Lock(), executor=engine, clock=fake_clock
        )
        monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
        get_health_registry().register(FixtureHealth())
        assert (await client.post("/api/v1/emergency", json=body)).status_code != 200

        async def unexpected(*args, **kwargs):
            raise RuntimeError("sensitive external response must not enter audit")

        monkeypatch.setattr("app.api.emergency.review_worker", unexpected)
        with pytest.raises(RuntimeError):
            await client.post(
                "/api/v1/emergency",
                json=body | {"action": "REVIEW_WORKER", "confirmation": "REVIEW PAPER WORKER"},
            )
        assert (await controls.restore())["kill_switch"]
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            rows = list(
                await session.scalars(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "EMERGENCY_REQUEST_REFUSED"
                    )
                )
            )
        assert len(rows) == 3
        assert any(row.mode == TradingMode.SUPERVISED for row in rows)
        assert any(row.result["code"] == "EMERGENCY_CLEAR_HEALTH_CHECKS_FAILED" for row in rows)
        failed = next(row for row in rows if row.decision == "FAILED")
        assert failed.result["code"] == "RuntimeError"
        assert "sensitive external response" not in str([row.result for row in rows])
    finally:
        await client.aclose()


async def test_refused_controls_have_queryable_evidence_without_state_claims(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, _, _, client, _ = await setup_execution(credentials, fake_clock)
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: None)
    try:
        body = {
            "action": "KILL",
            "reason": "Owner tests refused emergency requests",
            "confirmation": "wrong secret confirmation",
        }
        assert (await client.post("/api/v1/emergency", json=body)).status_code == 422
        response = await client.post(
            "/api/v1/emergency",
            json=body | {"action": "CLEAR", "confirmation": "CLEAR PAPER EMERGENCY"},
        )
        assert response.status_code != 200
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            rows = list(
                await session.scalars(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "EMERGENCY_REQUEST_REFUSED"
                    )
                )
            )
        assert len(rows) == 2
        assert all(row.actor == "owner" and row.decision == "REJECTED" for row in rows)
        assert {row.result["code"] for row in rows} == {
            "Typed emergency confirmation does not match",
            "RUNNING_RECOVERED_WORKER_REQUIRED",
        }
        for row in rows:
            assert await AuditService().verify(row.chain_id, expected_count=1)
            response = await client.get(f"/api/v1/audit/trails/{row.id}")
            assert response.status_code == 200
            assert response.json()["chains"][0]["integrity"] == "CONSISTENT"
            assert "wrong secret confirmation" not in response.text
        assert not (await EmergencyControls().restore())["entries_blocked"]
        response = await client.post(
            "/api/v1/emergency", json=body | {"action": "FLATTEN", "confirmation": "FLATTEN PAPER"}
        )
        assert response.json()["execution"] == "WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED"
        assert (await EmergencyControls().restore())["entries_blocked"]
        async with db_session.session_scope() as session:
            refused = await session.scalar(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.event_type == "EMERGENCY_REQUEST_REFUSED",
                    AuditEvent.result.is_not(None),
                )
                .order_by(AuditEvent.id.desc())
            )
        assert refused.result["outcome"] == "ENTRIES_BLOCKED_FLATTEN_NOT_EXECUTED"
    finally:
        await client.aclose()


async def test_refusal_audit_failure_never_executes_control(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, _, _, client, _ = await setup_execution(credentials, fake_clock)

    async def unavailable(**kwargs):
        raise RuntimeError("isolated audit storage failure")

    monkeypatch.setattr("app.api.emergency.record_refusal", unavailable)
    try:
        with pytest.raises(RuntimeError, match="isolated audit storage failure"):
            await client.post(
                "/api/v1/emergency",
                json={
                    "action": "KILL",
                    "reason": "Owner tests unavailable audit storage",
                    "confirmation": "wrong",
                },
            )
        assert await engine.broker.list_orders() == []
        assert not (await EmergencyControls().restore())["kill_switch"]
        with pytest.raises(SafetyError, match="ACTOR_REASON"):
            await EmergencyControls().clear(None, actor="", reason="          ")
    finally:
        await client.aclose()


class FixtureHealth(HealthCheck):
    name = "isolated-emergency-test"
    critical = True
    status = HealthStatus.FAIL

    async def run(self):
        return self.status, "Isolated deterministic health fixture", {}


async def test_owner_kill_survives_gate_reset_and_blocks_approved_order(
    db_engine, credentials, fake_clock
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    body = {
        "action": "KILL",
        "reason": "Owner requests emergency inhibition",
        "confirmation": "KILL PAPER",
    }
    try:
        bad = await client.post("/api/v1/emergency", json=body | {"confirmation": "yes"})
        assert bad.status_code == 422
        response = await client.post("/api/v1/emergency", json=body)
        assert response.status_code == 200
        assert response.json()["kill_switch"]
        get_trading_gate().clear("emergency")
        assert (await EmergencyControls(fake_clock).restore())["entries_blocked"]
        get_trading_gate().clear("emergency")
        with pytest.raises(SafetyError, match="EMERGENCY_ENTRIES_BLOCKED"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
        assert await AuditService().verify("paper-emergency", expected_count=1)
        client.headers.pop("Authorization")
        assert (await client.post("/api/v1/emergency", json=body)).status_code == 401
    finally:
        await client.aclose()


async def test_authenticated_flatten_uses_real_exit_and_is_idempotent(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        worker = SimpleNamespace(cycle_lock=asyncio.Lock(), executor=engine, clock=fake_clock)
        monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
        market.update(bid=Decimal(101), ask=Decimal("101.05"))
        body = {
            "action": "FLATTEN",
            "reason": "Owner requests paper emergency flatten",
            "confirmation": "FLATTEN PAPER",
        }
        response = await client.post("/api/v1/emergency", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["outcomes"][0]["kind"] == "EXIT"
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        assert position.net_quantity == 0
        assert position.exit_reason.value == "EMERGENCY"
        assert position.realised_pnl == 250
        assert (await client.post("/api/v1/emergency", json=body)).status_code == 200
        assert len(await engine.broker.list_orders()) == 2
    finally:
        await client.aclose()


async def test_flatten_without_worker_does_not_claim_execution(db_engine, credentials, fake_clock):
    _, _, _, client, _ = await setup_execution(credentials, fake_clock)
    try:
        response = await client.post(
            "/api/v1/emergency",
            json={
                "action": "FLATTEN",
                "reason": "Owner requests flatten with worker absent",
                "confirmation": "FLATTEN PAPER",
            },
        )
        assert response.status_code == 200
        assert response.json()["execution"] == "WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED"
        assert response.json()["outcomes"] == []
        async with db_session.session_scope() as session:
            state = await session.get(SystemState, SINGLETON_ID)
            state.new_entries_blocked = False
        with pytest.raises(SafetyError, match="INTEGRITY"):
            await EmergencyControls().restore()
    finally:
        await client.aclose()


async def test_clear_requires_fresh_health_and_preserves_other_blockers(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, _, _, client, _ = await setup_execution(credentials, fake_clock)
    worker = SimpleNamespace(
        running=True, cycle_lock=asyncio.Lock(), executor=engine, clock=fake_clock
    )
    controls = EmergencyControls(fake_clock)
    try:
        await controls.activate("KILL", actor="owner", reason="Isolated kill and reset test")
        with pytest.raises(SafetyError, match="HEALTH_CHECKS_FAILED"):
            await controls.clear(worker, actor="owner", reason="Review reset with no checks")
        health = FixtureHealth()
        get_health_registry().register(health)
        with pytest.raises(SafetyError, match="HEALTH_CHECKS_FAILED"):
            await controls.clear(worker, actor="owner", reason="Review reset with failing check")
        assert (await controls.restore())["kill_switch"]
        health.status = HealthStatus.PASS
        get_trading_gate().block("other-test-risk", "OTHER_LATCH_REMAINS")
        monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
        response = await client.post(
            "/api/v1/emergency",
            json={
                "action": "CLEAR",
                "reason": "Owner reviewed and clears emergency only",
                "confirmation": "CLEAR PAPER EMERGENCY",
            },
        )
        assert response.status_code == 200, response.text
        assert not (await controls.restore())["entries_blocked"]
        assert "OTHER_LATCH_REMAINS" in get_trading_gate().reason()
        assert await AuditService().verify("paper-emergency", expected_count=2)
    finally:
        await client.aclose()


async def test_stale_flatten_does_not_fabricate_exit_and_keeps_blocker(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        market["observed"] -= timedelta(minutes=2)
        worker = SimpleNamespace(cycle_lock=asyncio.Lock(), executor=engine, clock=fake_clock)
        monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
        response = await client.post(
            "/api/v1/emergency",
            json={
                "action": "FLATTEN",
                "reason": "Owner requests flatten with stale source",
                "confirmation": "FLATTEN PAPER",
            },
        )
        assert response.status_code == 200
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        assert position.net_quantity == 250
        assert (await EmergencyControls().restore())["entries_blocked"]
        assert response.json()["execution"] != "ALL_POSITIONS_CLOSED"
    finally:
        await client.aclose()
