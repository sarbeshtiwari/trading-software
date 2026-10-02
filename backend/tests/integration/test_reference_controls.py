"""Actual authenticated control endpoints; no unauthenticated mutation shortcut."""

import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.strategies.registry import StrategyRegistry
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator

__all__ = ["credentials"]


async def test_owner_register_enable_and_publish_audited_inputs(db_engine, credentials, fake_clock):
    _executor, _proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    try:
        reason = "Owner configures isolated reference fixture"
        body = {"instrument_id": "ins-test", "reason": reason}
        response = await client.post("/api/v1/strategies/reference/register", json=body)
        assert response.status_code == 200, response.text
        registered = await StrategyRegistry().get("closed-candle-breakout", "1")
        assert not registered.enabled_paper and not registered.enabled_live
        enabled = await client.post(
            "/api/v1/strategies/closed-candle-breakout/paper",
            json={
                "version": "1",
                "enabled": True,
                "reason": reason,
            },
        )
        assert enabled.status_code == 200
        inputs = {
            "costs": context.costs.model_dump(mode="json"),
            "validation": validator().policy.model_dump(mode="json"),
            "ban_listed": False,
            "news_halt": False,
            "manually_blocked": False,
        }
        response = await client.post(
            "/api/v1/strategies/reference/inputs",
            json={
                "inputs": inputs,
                "reason": reason,
            },
        )
        assert response.status_code == 200, response.text
        async with db_session.session_scope() as session:
            events = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type.in_(
                                ["STRATEGY_REGISTERED", "STRATEGY_ENABLEMENT", "REFERENCE_INPUTS"]
                            ),
                            AuditEvent.actor == "owner",
                        )
                    )
                ).all()
            )
        assert len(events) == 3
        assert all(event.result["reason"] == reason for event in events)
        client.headers.pop("Authorization")
        assert (
            await client.post("/api/v1/strategies/reference/register", json=body)
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/strategies/reference/inputs",
                json={
                    "inputs": inputs,
                    "reason": reason,
                },
            )
        ).status_code == 401
    finally:
        await client.aclose()
