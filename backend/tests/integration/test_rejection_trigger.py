"""Actual deterministic PAPER broker rejection drives durable controls and safe exits."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.enums import ExitReason
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Position
from app.emergency.controls import EmergencyControls
from app.execution.paper import PaperExecution
from app.notifications.outbox import NotificationOutbox
from tests.integration.test_notification_outbox import RecordingChannel, delivery, requests
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


async def test_distinct_rejections_trip_once_survive_restart_and_block_approved_order(
    db_engine, credentials, fake_clock
):
    engine, proposal, _, client, context = await setup_execution(credentials, fake_clock)
    engine.settings.paper_broker_rejection_limit = 2
    engine.broker._engine.config = replace(engine.broker._engine.config, reject_probability=1)
    try:
        first = await engine.submit(proposal)
        await engine.sync(first)
        assert not (await EmergencyControls().restore())["entries_blocked"]
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        second = await quant_decision(DecisionPipeline(validator()), payload(), context)
        third = await quant_decision(DecisionPipeline(validator()), payload(), context)
        await restored.submit(second.proposal_id)
        assert (await EmergencyControls().restore())["entries_blocked"]
        with pytest.raises(SafetyError, match="EMERGENCY"):
            await restored.submit(third.proposal_id)
        assert len(await restored.broker.list_orders()) == 2
        async with db_session.session_scope() as session:
            events = list(
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.event_type == "BROKER_REJECTION_OBSERVED")
                    .order_by(AuditEvent.sequence)
                )
            )
        assert [event.result["count"] for event in events] == [1, 2]
        assert events[-1].result["limit"] == 2
        assert await AuditService().verify("paper-emergency", expected_count=1)
        channel = RecordingChannel()
        await NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock).dispatch_once()
        assert (
            len(
                [
                    notice
                    for notice in channel.messages
                    if notice.event_type == "BROKER_REJECTION_LIMIT"
                ]
            )
            == 1
        )
        fake_clock.advance(timedelta(days=1))
        await restored.sync(first)
        assert (
            len(
                [
                    row
                    for row in await requests()
                    if row.result["notification"]["event_type"] == "BROKER_REJECTION_LIMIT"
                ]
            )
            == 1
        )
        await restored.recover()
        assert (await EmergencyControls().restore())["entries_blocked"]
    finally:
        await client.aclose()


async def test_trigger_and_order_sync_roll_back_together_then_recover(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    engine.settings.paper_broker_rejection_limit = 1
    engine.broker._engine.config = replace(engine.broker._engine.config, reject_probability=1)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                "app.emergency.rejections.enqueue",
                AsyncMock(side_effect=RuntimeError("isolated trigger outbox failure")),
            )
            with pytest.raises(RuntimeError, match="trigger outbox failure"):
                await engine.submit(proposal)
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "BROKER_REJECTION_OBSERVED")
                )
                == 0
            )
        assert not (await EmergencyControls().restore())["entries_blocked"]
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restored.recover()
        assert (await EmergencyControls().restore())["entries_blocked"]
        assert len(await restored.broker.list_orders()) == 1
        assert await AuditService().verify("paper-emergency", expected_count=1)
    finally:
        await client.aclose()


async def test_exit_rejection_latches_entries_but_safe_exit_retry_remains_available(
    db_engine, credentials, fake_clock
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    engine.settings.paper_broker_rejection_limit = 1
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
        engine.broker._engine.config = replace(engine.broker._engine.config, reject_probability=1)
        await engine.exit(position.id, ExitReason.EMERGENCY)
        assert (await EmergencyControls().restore())["entries_blocked"]
        engine.broker._engine.config = replace(engine.broker._engine.config, reject_probability=0)
        await engine.exit(position.id, ExitReason.EMERGENCY, retry_terminal=True)
        async with db_session.session_scope() as session:
            assert (await session.get(Position, position.id)).net_quantity == 0
        assert (await EmergencyControls().restore())["entries_blocked"]
    finally:
        await client.aclose()
