"""Production PAPER lifecycle and durable notice delivery; transports are test fixtures only."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.notifications.models import DeliveryPolicy
from app.notifications.outbox import NotificationOutbox
from app.notifications.routing import ChannelRoute, RoutingPolicy
from app.notifications.service import NotificationService
from tests.integration.test_auth import credentials
from tests.integration.test_reference_worker import setup_worker
from tests.unit.test_notifications import RecordingChannel

__all__ = ["credentials"]


def delivery(channel, clock):
    return NotificationService(
        {"email": channel},
        RoutingPolicy(
            routes=(ChannelRoute(channel="email", severities=("INFO", "WARNING", "CRITICAL")),)
        ),
        DeliveryPolicy(max_attempts=2, retry_delay_seconds=0),
        clock=clock,
    )


async def requests():
    async with db_session.session_scope() as session:
        return list(
            (
                await session.scalars(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "NOTIFICATION_REQUESTED",
                    )
                )
            ).all()
        )


async def test_ingestion_strategy_costed_trade_delivers_durable_lifecycle_notices(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    channel = RecordingChannel()
    try:
        await worker.cycle()
        assert not worker.failed
        await worker.cycle()
        assert len(await requests()) == 1
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            ltp=Decimal(108),
            bids=(replace(provider.get_quote.return_value.bids[0], price=Decimal(108)),),
            asks=(replace(provider.get_quote.return_value.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed
        rows = await requests()
        assert len(rows) == 2
        assert (
            await NotificationOutbox(
                delivery(channel, fake_clock), clock=fake_clock
            ).dispatch_once()
            == 2
        )
        assert len(channel.messages) == 2
        assert {notice.event_id for notice in channel.messages} == {row.chain_id for row in rows}
        assert all("PAPER SIMULATED" in notice.message for notice in channel.messages)
        restored = NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock)
        assert await restored.dispatch_once() == 0
        for row in rows:
            assert await AuditService(fake_clock).verify(row.chain_id)
            async with db_session.session_scope() as session:
                source = await session.get(AuditEvent, row.result["source_audit_id"])
            assert source.order_id == row.order_id and source.position_id == row.position_id
        state = (await client.get("/api/v1/workspace")).json()
        assert Decimal(state["journal"][0]["net_pnl"]) == Decimal("856.56")
        assert len(state["notifications"]) == 2
        assert all(row["channels"] == {"email": "ACKNOWLEDGED"} for row in state["notifications"])
        assert not any(row["external_delivery_verified"] for row in state["notifications"])
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("recover", [True, False])
async def test_failed_delivery_recovers_without_changing_trade_or_unbounded_retries(
    db_engine, credentials, fake_clock, tmp_path, recover
):
    worker, _, client = await setup_worker(credentials, fake_clock, tmp_path)

    class FailingChannel(RecordingChannel):
        async def send(self, notification):
            raise RuntimeError("isolated notification failure")

    try:
        await worker.cycle()
        failed = NotificationOutbox(delivery(FailingChannel(), fake_clock), clock=fake_clock)
        assert await failed.dispatch_once() == 1
        assert not worker.failed
        healthy = RecordingChannel()
        restored = NotificationOutbox(
            delivery(healthy if recover else FailingChannel(), fake_clock), clock=fake_clock
        )
        assert await restored.dispatch_once() == 1
        assert len(healthy.messages) == (1 if recover else 0)
        assert await restored.dispatch_once() == 0
        state = (await client.get("/api/v1/workspace")).json()
        assert len(state["orders"]) == 1 and state["positions"][0]["net_quantity"] == 125
        assert state["notifications"][0]["attempts"] == 2
        assert state["notifications"][0]["channels"]["email"] == (
            "ACKNOWLEDGED" if recover else "FAILED"
        )
    finally:
        await worker.stop()
        await client.aclose()


async def test_interrupted_send_uses_expiring_claim_and_persisted_attempt_budget(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _, client = await setup_worker(credentials, fake_clock, tmp_path)

    class InterruptedChannel(RecordingChannel):
        async def send(self, notification):
            raise asyncio.CancelledError()

    try:
        await worker.cycle()
        with pytest.raises(asyncio.CancelledError):
            await NotificationOutbox(
                delivery(InterruptedChannel(), fake_clock), clock=fake_clock
            ).dispatch_once()
        channel = RecordingChannel()
        restored = NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock)
        assert await restored.dispatch_once() == 0
        fake_clock.advance_seconds(16)
        assert await restored.dispatch_once() == 1
        assert len(channel.messages) == 1
        assert await restored.dispatch_once() == 0
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("malformed", [False, True])
async def test_altered_notice_is_not_delivered(db_engine, credentials, fake_clock, tmp_path, malformed):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    channel = RecordingChannel()
    try:
        await worker.cycle()
        row = (await requests())[0]
        async with db_session.session_scope() as session:
            stored = await session.get(AuditEvent, row.id)
            stored.result = stored.result | {
                "notification": stored.result["notification"]
                | {"message": "isolated tampered fixture"}
            }
            if malformed:
                stored.result = {"notification": None}
        outbox = NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock)
        assert await outbox.dispatch_once() == 0
        assert channel.messages == []
        state = (await client.get("/api/v1/workspace")).json()
        assert state["notifications"][0]["status"] == "INTEGRITY_FAILURE"
        assert state["notifications"][0]["channels"] == {}
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            ltp=Decimal(108),
            bids=(replace(provider.get_quote.return_value.bids[0], price=Decimal(108)),),
            asks=(replace(provider.get_quote.return_value.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert await outbox.dispatch_once() == 1
        assert len(channel.messages) == 1
        assert channel.messages[0].event_id != row.chain_id
        restored = NotificationOutbox(delivery(channel, fake_clock), clock=fake_clock)
        assert await restored.dispatch_once() == 0
        state = (await client.get("/api/v1/workspace")).json()
        assert len(state["notifications"]) == 2
        assert {notice["status"] for notice in state["notifications"]} == {
            "INTEGRITY_FAILURE", "RECORDED_CHANNEL_OUTCOMES"
        }
        assert not worker.failed
    finally:
        await worker.stop()
        await client.aclose()
