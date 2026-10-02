"""Owner review of a genuinely interrupted producer never replays its approved intent."""

import asyncio

import pytest
import sqlalchemy as sa

from app.core.enums import HealthStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.monitoring.healthchecks import get_health_registry
from app.trading.review import review_worker
from tests.integration.test_auth import credentials
from tests.integration.test_emergency import FixtureHealth
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


async def test_interrupted_approval_is_discarded_not_dispatched(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _, client = await setup_worker(credentials, fake_clock, tmp_path)
    original = worker.reference_runtime._record

    async def interrupt(*args, **kwargs):
        raise asyncio.CancelledError()

    try:
        monkeypatch.setattr(worker.reference_runtime, "_record", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await worker.cycle()
        monkeypatch.setattr(worker.reference_runtime, "_record", original)
        await worker.cycle()
        assert worker.failed
        health = FixtureHealth()
        health.status = HealthStatus.PASS
        get_health_registry().register(health)
        monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
        response = await client.post(
            "/api/v1/emergency",
            json={
                "action": "REVIEW_WORKER",
                "confirmation": "REVIEW PAPER WORKER",
                "reason": "Owner reviewed interrupted producer evidence",
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["review"]
        assert len(result["discarded_proposals"]) == 1
        assert len(result["resolved_claims"]) == 1
        assert not worker.failed
        await worker.cycle()
        assert not worker.failed
        async with db_session.session_scope() as session:
            incidents = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.event_type == "PAPER_INCIDENT_TRANSITION")
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )
            assert [
                row.result["active"] for row in incidents if row.result["kind"] == "WORKER_FAILURE"
            ] == [True, False]
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            proposal = await session.get(Proposal, result["discarded_proposals"][0])
            assert proposal.status == "EXECUTION_BLOCKED"
            assert await session.scalar(
                sa.select(AuditEvent.id).where(
                    AuditEvent.event_type == "PAPER_WORKER_REVIEWED",
                    AuditEvent.actor == "owner",
                )
            )
    finally:
        await worker.stop()
        await client.aclose()


async def test_worker_review_refuses_open_position(db_engine, credentials, fake_clock, tmp_path):
    worker, _, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        assert not worker.failed
        health = FixtureHealth()
        health.status = HealthStatus.PASS
        get_health_registry().register(health)
        with pytest.raises(SafetyError, match="FLAT_RECONCILED_ACCOUNT"):
            await review_worker(
                worker, actor="owner", reason="Owner cannot waive open exposure review"
            )
    finally:
        await worker.stop()
        await client.aclose()
