"""Offline recovery uses historical fill seals and excludes later exits."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Position, Trade
from app.execution import paper as paper_module
from app.monitoring.gate import get_trading_gate
from tests.integration.test_auth import credentials
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


@pytest.mark.parametrize("evidence", ["sealed", "legacy", "corrupt"])
async def test_worker_recovers_past_open_position_not_todays_closed_state(
    db_engine, credentials, fake_clock, tmp_path, evidence
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        day = fake_clock.now().date()
        await worker.cycle()
        async with db_session.session_scope() as session:
            position_id = await session.scalar(sa.select(Position.id))
            fill_id = await session.scalar(sa.select(Trade.id))
            if evidence == "legacy":
                await session.execute(sa.delete(AuditEvent).where(AuditEvent.chain_id == fill_id))
            elif evidence == "corrupt":
                await session.execute(
                    sa.update(AuditEvent)
                    .where(AuditEvent.chain_id == fill_id)
                    .values(result={"corrupt": True})
                )
        await worker.stop()
        fake_clock.advance(timedelta(days=1))
        while fake_clock.now().weekday() >= 5:
            fake_clock.advance(timedelta(days=1))
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            observed_at=fake_clock.now(),
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.start(schedule=False)
        await worker.executor.exit(position_id)
        get_trading_gate().block("fixture_no_new_entries", "Isolated recovery observation")
        if evidence == "corrupt":
            with pytest.raises(ValueError, match="fill audit integrity"):
                await worker.cycle()
            assert worker.failed
            return
        await worker.cycle()
        async with db_session.session_scope() as session:
            current = await session.get(Position, position_id)
            assert current.net_quantity == 0
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "PAPER_DAILY_SUMMARY_CATCHUP"
                        )
                    )
                ).all()
            )
        assert len(records) == 1
        summary = records[0].result
        assert summary["session_date"] == day.isoformat()
        assert summary["worker_review_required"] is None
        assert summary["reconciliation"] == "NOT_CONFIRMED"
        assert not summary["order_history_available"]
        if evidence == "sealed":
            assert summary["fill_ids"] == [fill_id]
            assert summary["open_positions"][0]["quantity"] == 111
            assert Decimal(summary["gross_realised_pnl"]) == 0
            assert Decimal(summary["charges"]) == Decimal("13.84")
            assert Decimal(summary["net_realised_pnl"]) == Decimal("-13.84")
        else:
            assert summary["fill_count"] is None
            assert summary["net_realised_pnl"] is None
            assert not summary["position_history_available"]
        assert await AuditService(fake_clock).verify(records[0].chain_id, expected_count=1)
        await worker.stop()
        await worker.start(schedule=False)
        await worker.cycle()
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "PAPER_DAILY_SUMMARY_CATCHUP")
                )
                == 1
            )
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": credentials[1]},
            headers={"X-Requested-With": "ATS"},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/reports/paper/daily")
        assert response.status_code == 200, response.text
        assert response.json()["reports"][0]["summary"]["scope"] == "MISSED_SESSION_RECOVERY"
    finally:
        await worker.stop()
        await client.aclose()


async def test_fill_seal_failure_rolls_back_local_accounting_then_recovers_once(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)

    async def unavailable_seal(*args, **kwargs):
        raise OSError("isolated audit storage failure")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(paper_module, "record_fill", unavailable_seal)
            await worker.cycle()
        assert worker.failed
        assert len(await worker.executor.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
        await worker.stop()
        await worker.start(schedule=False)
        assert worker.failed
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
            seal = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "PAPER_FILL_RECORDED")
            )
        assert seal is not None
        assert await AuditService(fake_clock).verify(seal.chain_id, expected_count=1)
        assert len(await worker.executor.broker.list_orders()) == 1
    finally:
        await worker.stop()
        await client.aclose()
