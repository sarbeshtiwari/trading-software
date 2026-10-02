"""Authenticated controls use persisted evidence, never a client-supplied account."""

import httpx
import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.core.clock import UTC
from app.db import session as db_session
from app.db.models.audit import AuditEvent, ConfigChange
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.system import SINGLETON_ID, SystemState
from app.main import create_app
from app.monitoring.gate import get_trading_gate
from app.risk.safety import RiskSafety
from app.security.auth import OwnerAuth
from tests.integration.test_auth import credentials
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_risk import limits, market, portfolio

__all__ = ["credentials"]


@pytest.fixture(autouse=True)
def now(fake_clock):
    fake_clock.set_to(OBSERVED)


async def test_authenticated_rearm_preserves_daily_and_unrelated_blocks(
    db_engine, credentials, fake_clock
):
    _, password = credentials
    access, _ = await OwnerAuth().login("owner", password)
    headers = {"Authorization": f"Bearer {access}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test", headers=headers
    ) as client:
        change = {
            "limits": limits().model_dump(mode="json"),
            "expected_version": 0,
            "reason": "Explicit test-only owner configuration",
        }
        assert (await client.post("/api/v1/risk/configuration", json=change)).status_code == 200
        safety = RiskSafety()
        await safety.observe(portfolio(realised_day_pnl=-2000, equity=90000), market(), limits())
        await safety.trip_error("PAPER", "SYNTHETIC")
        body = {
            "origin": "SYNTHETIC",
            "reason": "Reviewed synthetic test account recovery",
            "confirmation": "REARM PAPER RISK",
        }
        assert (await client.post("/api/v1/risk/rearm", json=body)).status_code == 409
        async with db_session.session_scope() as session:
            session.add(
                SystemState(
                    id=SINGLETON_ID,
                    last_reconciliation_at=fake_clock.now().astimezone(UTC),
                    open_discrepancies=0,
                )
            )
        assert (await client.post("/api/v1/risk/rearm", json=body)).status_code == 423
        await safety.observe(portfolio(), market(), limits())
        get_trading_gate().block("reconciliation", "unrelated manual review")
        result = await client.post("/api/v1/risk/rearm", json=body)
        assert result.status_code == 200, result.text
        state = result.json()["state"]
        assert state["daily_loss"] and not state["drawdown"] and not state["engine_error"]
        assert not result.json()["gate"]["new_entries_allowed"]
        assert "unrelated manual review" in get_trading_gate().reason()
        forged = await client.post(
            "/api/v1/risk/rearm", json=body | {"portfolio": {"equity": 999999}}
        )
        assert forged.status_code == 422
        assert (await client.post("/api/v1/risk/configuration", json=change)).status_code == 409
    async with db_session.session_scope() as session:
        changes = list((await session.scalars(sa.select(ConfigChange))).all())
        intents = await session.scalar(
            sa.select(sa.func.count()).select_from(RuntimeEventOutbox)
            .join(AuditEvent, AuditEvent.id == RuntimeEventOutbox.audit_id)
            .where(AuditEvent.event_type == "RISK_REARM")
        )
        assert intents == 1
    assert {item.action for item in changes} == {"ACTIVATE", "REARM"}
    assert all(item.actor == "owner" and item.reason for item in changes)


async def test_active_configuration_is_enforced_by_real_pipeline(db_engine, credentials):
    context = await setup_context()
    _, password = credentials
    access, _ = await OwnerAuth().login("owner", password)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {access}"},
    ) as client:
        response = await client.post(
            "/api/v1/risk/configuration",
            json={
                "limits": limits(per_trade_risk_pct="0.25").model_dump(mode="json"),
                "expected_version": 0,
                "reason": "Reduce test-only owner risk budget",
            },
        )
        assert response.status_code == 200
    result = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert result.approved_quantity == 0 and result.code == "RISK_CONFIG_VERSION_MISMATCH"
