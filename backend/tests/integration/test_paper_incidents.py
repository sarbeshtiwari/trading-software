"""Actual PAPER failures produce durable notices without remote delivery claims."""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.core.calendar import TradingCalendar
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Position
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate
from app.notifications import incidents as incident_module
from app.notifications.incidents import PaperIncidents
from app.notifications.outbox import NotificationOutbox
from app.trading.worker import PaperWorker
from tests.integration.test_notification_outbox import delivery, requests
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.unit.test_notifications import RecordingChannel

__all__ = ["credentials"]


async def test_reconciliation_incident_survives_rollback_restart_and_recurrence(
    db_engine, credentials, fake_clock
):
    engine, proposal_id, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal_id)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            identifier, quantity = position.id, position.net_quantity
            position.net_quantity += 1
        for executor in (
            engine,
            engine,
            PaperExecution(engine.source, settings=engine.settings, clock=fake_clock),
        ):
            with pytest.raises(SafetyError, match="RECONCILE"):
                await executor.recover()
        notices = [
            row
            for row in await requests()
            if row.result["notification"]["event_type"] == "RECONCILIATION_DISCREPANCY"
        ]
        assert len(notices) == 1
        assert notices[0].result["notification"]["severity"] == "CRITICAL"
        assert not get_trading_gate().new_entries_allowed
        async with db_session.session_scope() as session:
            (await session.get(Position, identifier)).net_quantity = quantity
        await engine.recover()
        async with db_session.session_scope() as session:
            (await session.get(Position, identifier)).net_quantity += 1
        with pytest.raises(SafetyError, match="RECONCILE"):
            await engine.recover()
        notices = [
            row
            for row in await requests()
            if row.result["notification"]["event_type"] == "RECONCILIATION_DISCREPANCY"
        ]
        assert len(notices) == 3
        channel = RecordingChannel()
        await NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock).dispatch_once()
        assert (
            len(
                [
                    notice
                    for notice in channel.messages
                    if notice.event_type == "RECONCILIATION_DISCREPANCY"
                ]
            )
            == 3
        )
        state = (await client.get("/api/v1/workspace")).json()
        assert any(
            row["event_type"] == "RECONCILIATION_DISCREPANCY" for row in state["notifications"]
        )
        assert not any(row["external_delivery_verified"] for row in state["notifications"])
    finally:
        await client.aclose()


async def test_stale_feed_worker_notices_are_durable_and_not_repeated(
    db_engine, credentials, fake_clock, tmp_path
):
    engine, _proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "incident.lock",
    )
    try:
        await worker.start(schedule=False)
        await worker.cycle()
        fake_clock.advance(timedelta(seconds=20))
        await worker.cycle()
        await worker.cycle()
        assert worker.failed
        notices = [row.result["notification"] for row in await requests()]
        for kind in ("WORKER_FAILURE", "FEED_OUTAGE"):
            matching = [row for row in notices if row["event_type"] == kind]
            assert len(matching) == 1 and matching[0]["severity"] == "CRITICAL"
            await PaperIncidents(fake_clock).observe(kind, active=True)
        assert len(await requests()) == len(notices)
    finally:
        await worker.stop()
        await client.aclose()


async def test_incident_outbox_failure_rolls_back_transition_and_blocks(
    db_engine, fake_clock, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("isolated outbox storage failure")

    monkeypatch.setattr(incident_module, "enqueue", unavailable)
    with pytest.raises(RuntimeError):
        await PaperIncidents(fake_clock).observe("WORKER_FAILURE", active=True)
    assert "PAPER_INCIDENT_AUDIT_UNAVAILABLE" in get_trading_gate().reason()
    assert await requests() == []
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 0
