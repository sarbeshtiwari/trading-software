"""Real worker exit policies, persisted state and OMS; only market input is synthetic."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.security.auth import OwnerAuth
from app.trading.worker import PaperWorker
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials"]


def quote_at(provider, fake_clock, price):
    quote = provider.get_quote.return_value
    price = Decimal(price)
    provider.get_quote.return_value = replace(
        quote,
        ltp=price,
        observed_at=fake_clock.now(),
        bids=(replace(quote.bids[0], price=price),),
        asks=(replace(quote.asks[0], price=price + Decimal("0.05")),),
    )


@pytest.mark.parametrize("reason", ["TRAILING_STOP", "TIME_EXIT", "INVALIDATION"])
async def test_reference_exit_policy_reaches_actual_journal(
    db_engine, credentials, fake_clock, tmp_path, reason
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        if reason == "TRAILING_STOP":
            quote_at(provider, fake_clock, "106")
            await worker.cycle()
            assert not worker.failed, worker.detail
            state = (await client.get("/api/v1/workspace")).json()
            assert Decimal(state["positions"][0]["trailing_stop_price"]) == 102
            await worker.stop()
            engine = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
            worker = PaperWorker(
                engine,
                provider=provider,
                calendar=worker.calendar,
                lock_path=tmp_path / "reference-worker.lock",
            )
            await worker.start(schedule=False)
            fake_clock.advance_seconds(1)
            quote_at(provider, fake_clock, "101")
        elif reason == "TIME_EXIT":
            fake_clock.advance_seconds(3600)
            provider.get_candles.return_value = tuple(
                replace(bar, ts=bar.ts + timedelta(hours=1))
                for bar in provider.get_candles.return_value
            )
            quote_at(provider, fake_clock, "100")
        else:
            fake_clock.advance_seconds(60)
            bars = provider.get_candles.return_value
            provider.get_candles.return_value = (
                *bars[1:],
                replace(
                    bars[-1],
                    ts=bars[-1].ts + timedelta(minutes=1),
                    open=Decimal(98),
                    high=Decimal(98),
                    low=Decimal(97),
                    close=Decimal(97),
                ),
            )
            quote_at(provider, fake_clock, "100")
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            journal = await session.scalar(sa.select(JournalEntry))
            assert position.net_quantity == 0 and journal.exit_reason.value == reason
            assert journal.gross_pnl == (125 if reason == "TRAILING_STOP" else 0)
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
            record = await session.scalar(
                sa.select(AuditEvent)
                .where(AuditEvent.event_type == "REFERENCE_EXIT_STATE")
                .order_by(AuditEvent.sequence.desc())
            )
            assert record.correlation_id
            assert (
                record.result["reason"]
                == {
                    "TRAILING_STOP": "TRAILING",
                    "TIME_EXIT": "TIME",
                    "INVALIDATION": "INVALIDATION",
                }[reason]
            )
        if reason != "TRAILING_STOP":
            access, _ = await OwnerAuth().login("owner", credentials[1])
            client.headers["Authorization"] = f"Bearer {access}"
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert Decimal(response.json()["account"]["gross_exposure"]) == 0
        assert Decimal(response.json()["account"]["realised_pnl"]) == journal.gross_pnl
    finally:
        await worker.stop()
        await client.aclose()


async def test_missing_analytics_blocks_worker_but_fixed_stop_still_exits(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        provider.get_candles.side_effect = RuntimeError("isolated market boundary failure")
        await worker.cycle()
        assert worker.failed
        async with db_session.session_scope() as session:
            assert (await session.scalar(sa.select(Position))).net_quantity == 125
            failure = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "REFERENCE_EXIT_FAILURE")
            )
            assert failure.position_id
            notices = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "NOTIFICATION_REQUESTED"
                        )
                    )
                ).all()
            )
            assert any(
                record.result["notification"]["event_type"] == "PAPER_EXIT_MONITOR_FAILURE"
                for record in notices
            )
        quote_at(provider, fake_clock, "95")
        await worker.cycle()
        async with db_session.session_scope() as session:
            assert (await session.scalar(sa.select(Position))).net_quantity == 0
            assert (await session.scalar(sa.select(JournalEntry))).exit_reason.value == "STOP_LOSS"
        assert worker.failed
    finally:
        await worker.stop()
        await client.aclose()


async def test_trailing_state_cannot_be_silently_loosened(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        quote_at(provider, fake_clock, "106")
        await worker.cycle()
        async with db_session.session_scope() as session:
            (await session.scalar(sa.select(Position))).trailing_stop_price = Decimal(96)
        quote_at(provider, fake_clock, "103")
        await worker.cycle()
        assert worker.failed and "REFERENCE_EXIT_STATE_CHANGED" in worker.detail
        assert len(await worker.executor.broker.list_orders()) == 1
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("exit_reason", ["TRAILING_STOP", "TIME_EXIT"])
async def test_valid_quote_still_allows_independent_exit_when_analytics_fail(
    db_engine, credentials, fake_clock, tmp_path, exit_reason
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        provider.get_candles.side_effect = RuntimeError("isolated analytics outage")
        if exit_reason == "TRAILING_STOP":
            quote_at(provider, fake_clock, "106")
            await worker.cycle()
            assert worker.failed
            fake_clock.advance_seconds(1)
            quote_at(provider, fake_clock, "101")
        else:
            fake_clock.advance_seconds(3600)
            quote_at(provider, fake_clock, "100")
        await worker.cycle()
        assert worker.failed
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.exit_reason.value == exit_reason
            assert (await session.scalar(sa.select(Position))).net_quantity == 0
            record = await session.scalar(
                sa.select(AuditEvent)
                .where(AuditEvent.event_type == "REFERENCE_EXIT_STATE")
                .order_by(AuditEvent.sequence.desc())
            )
            assert record.result["invalidation_checked"] is False
            assert record.data_used["invalidation"] is None
            assert record.data_used["quote"]["data_origin"] == "SYNTHETIC"
    finally:
        await worker.stop()
        await client.aclose()


async def test_exit_failure_survives_restart_before_regular_heartbeat(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        provider.get_candles.side_effect = RuntimeError("isolated analytics outage")
        with pytest.raises(SafetyError, match="REFERENCE_EXIT_INVALIDATION_UNAVAILABLE"):
            await worker.reference_exits.cycle()
        assert not worker.failed
        await worker.stop()
        provider.get_candles.side_effect = None
        engine = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
        worker = PaperWorker(
            engine,
            provider=provider,
            calendar=worker.calendar,
            lock_path=tmp_path / "reference-worker.lock",
        )
        await worker.start(schedule=False)
        assert worker.failed
        assert "PAPER_WORKER_REVIEW_REQUIRED" in engine.gate.reason()
    finally:
        await worker.stop()
        await client.aclose()
