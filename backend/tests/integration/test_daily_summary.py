"""Session-end notices consume actual PAPER fills, not fabricated performance."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.system import Heartbeat, PortfolioSnapshot
from app.db.models.trading import Position, Trade
from app.modes import TradingMode
from app.notifications import summary as summary_module
from app.notifications.valuation import session_valuation
from app.risk.active import active_limits
from tests.integration.test_auth import credentials
from tests.integration.test_reference_worker import setup_worker

__all__ = ["credentials"]


async def summaries():
    async with db_session.session_scope() as session:
        return list(
            (
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "PAPER_DAILY_SUMMARY")
                )
            ).all()
        )


@pytest.mark.parametrize("costed", [False, True])
async def test_worker_summary_actual_fills_costs_and_restart_deduplication(
    db_engine, credentials, fake_clock, tmp_path, costed
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=costed)
    try:
        await worker.cycle()
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not await summaries()
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now()
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        records = await summaries()
        assert len(records) == 1
        result = records[0].result
        assert result["fill_count"] == 2
        assert result["traded_position_count"] == 1
        assert Decimal(result["gross_realised_pnl"]) == (888 if costed else 1000)
        if costed:
            assert Decimal(result["charges"]) == Decimal("31.44")
            assert Decimal(result["net_realised_pnl"]) == Decimal("856.56")
            valuation = result["valuation"]
            assert valuation["status"] == "ESTIMATED"
            assert Decimal(valuation["equity"]) == Decimal("100856.56")
            assert Decimal(valuation["unrealised_pnl"]) == 0
            assert Decimal(result["limit_utilisation"]["daily_loss"]["limit"]) == 2000
            assert Decimal(result["limit_utilisation"]["daily_loss"]["amount"]) == 0
            assert Decimal(valuation["peak_equity"]) == Decimal("100874.16")
            assert Decimal(result["limit_utilisation"]["drawdown"]["amount"]) == Decimal("17.60")
            assert Decimal(result["limit_utilisation"]["exposure"]["amount"]) == 0
        else:
            assert result["charges"] is None
            assert result["net_realised_pnl"] is None
            assert result["valuation"]["reason"] == "ACCOUNT_CHARGES_UNAVAILABLE"
        assert result["open_positions"] == []
        assert result["reconciliation"] == "COMPLETED_THIS_CYCLE"
        assert result["limit_utilisation"]["positions_used"] == 0
        assert await AuditService(fake_clock).verify(records[0].chain_id, expected_count=1)
        await worker.stop()
        await worker.start(schedule=False)
        await worker.cycle()
        assert len(await summaries()) == 1
        async with db_session.session_scope() as session:
            notices = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "NOTIFICATION_REQUESTED"
                        )
                    )
                ).all()
            )
            daily = [
                notice
                for notice in notices
                if notice.result["notification"]["event_type"] == "DAILY_SUMMARY"
            ]
        assert len(daily) == 1
        assert daily[0].result["source_audit_id"] == records[0].id
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": credentials[1]},
            headers={"X-Requested-With": "ATS"},
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert any(
            notice["event_type"] == "DAILY_SUMMARY" and notice["source_audit_id"] == records[0].id
            for notice in response.json()["notifications"]
        )
        response = await client.get("/api/v1/reports/paper/daily")
        assert response.status_code == 200, response.text
        report = response.json()["reports"][0]
        assert report["audit_id"] == records[0].id
        assert report["summary"]["fill_count"] == 2
        assert report["summary"]["net_realised_pnl"] == result["net_realised_pnl"]
        assert (await client.get("/api/v1/reports/paper/daily?limit=101")).status_code == 422
        if costed:
            await AuditService(fake_clock).append(
                AuditIdentity(
                    chain_id="isolated-invalid-summary",
                    event_type="PAPER_DAILY_SUMMARY",
                    actor="isolated-test",
                    mode=TradingMode.PAPER,
                ),
                {"result": result | {"as_of": fake_clock.utcnow() + timedelta(hours=1)}},
            )
            rejected = await client.get("/api/v1/reports/paper/daily")
            assert rejected.status_code == 409
            assert rejected.json()["detail"] == "PAPER_SUMMARY_EVIDENCE_UNAVAILABLE"
    finally:
        await worker.stop()
        await client.aclose()


async def test_failed_close_reports_open_position_not_success(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        await worker.cycle()
        assert worker.failed
        records = await summaries()
        assert len(records) == 1
        result = records[0].result
        assert result["worker_review_required"]
        assert result["cycle_error"]
        assert result["reconciliation"] == "NOT_CONFIRMED"
        assert result["open_positions"][0]["quantity"] == 125
        assert result["net_realised_pnl"] is None
        assert result["limit_utilisation"]["daily_loss"] is None
    finally:
        await worker.stop()
        await client.aclose()


async def test_session_close_boundary_uses_ist_with_utc_clock(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        day = fake_clock.now().date()
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=29).astimezone(UTC))
        await worker.cycle()
        assert not await summaries()
        fake_clock.set_to(fake_clock.utcnow().replace(minute=0, hour=10))
        await worker.cycle()
        records = await summaries()
        assert len(records) == 1
        assert records[0].result["session_date"] == day.isoformat()
        assert records[0].result["fill_count"] == 0
        assert records[0].result["cost_status"] == "NO_FILLS"
        async with db_session.session_scope() as session:
            await session.execute(
                sa.update(AuditEvent)
                .where(AuditEvent.id == records[0].id)
                .values(result={"tampered": True})
            )
        with pytest.raises(ValueError, match="summary audit integrity"):
            await worker.cycle()
        assert worker.failed
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": credentials[1]},
            headers={"X-Requested-With": "ATS"},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/reports/paper/daily")
        assert response.status_code == 409
        assert response.json()["detail"] == "PAPER_SUMMARY_INTEGRITY_FAILURE"
    finally:
        await worker.stop()
        await client.aclose()


async def test_summary_and_outbox_rollback_preserves_review_across_restart(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path)

    async def unavailable_outbox(*args, **kwargs):
        raise OSError("isolated storage failure")

    try:
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        with monkeypatch.context() as patcher:
            patcher.setattr(summary_module, "enqueue", unavailable_outbox)
            with pytest.raises(OSError, match="storage failure"):
                await worker.cycle()
        assert not await summaries()
        async with db_session.session_scope() as session:
            assert (await session.get(Heartbeat, "paper-worker")).detail["failed"]
        await worker.stop()
        await worker.start(schedule=False)
        assert worker.failed
        await worker.cycle()
        records = await summaries()
        assert len(records) == 1
        assert records[0].result["worker_review_required"]
    finally:
        await worker.stop()
        await client.aclose()


async def test_failed_close_recovery_appends_instead_of_rewriting_summary(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await worker.cycle()
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=31))
        await worker.cycle()
        initial = (await summaries())[0]
        assert initial.result["reconciliation"] == "NOT_CONFIRMED"
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now()
        )
        await worker.cycle()
        await worker.cycle()
        async with db_session.session_scope() as session:
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == initial.chain_id)
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )
        assert len(records) == 2
        assert records[0].result == initial.result
        recovered = records[1].result
        assert recovered["previous_summary_id"] == initial.id
        assert recovered["open_positions"] == []
        assert recovered["reconciliation"] == "COMPLETED_THIS_CYCLE"
        assert recovered["worker_review_required"]
        assert recovered["valuation"]["status"] == "ESTIMATED"
        assert await AuditService(fake_clock).verify(initial.chain_id, expected_count=2)
    finally:
        await worker.stop()
        await client.aclose()


async def test_valuation_rejects_future_fills_and_stale_marks_ignores_future_peak(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await worker.cycle()
        now = fake_clock.utcnow()
        async with db_session.session_scope() as session:
            positions = list((await session.scalars(sa.select(Position))).all())
            trade = await session.scalar(sa.select(Trade))
            limits = await active_limits(session)
            future = PortfolioSnapshot(
                ts=now + timedelta(hours=1), mode=TradingMode.PAPER, equity=Decimal("999999")
            )
            session.add(future)
            await session.flush()
            arguments = {
                "positions": positions,
                "limits": limits,
                "cutoff": now,
                "net_realised": -trade.total_charges,
                "cycle_error": None,
            }
            value = await session_valuation(session, worker, **arguments)
            assert value["status"] == "ESTIMATED"
            assert value["peak_equity"] == 100000
            assert value["gross_exposure"] == 11100
            assert future.id not in value["peak_snapshot_ids"]
            trade.executed_at = now + timedelta(seconds=1)
            await session.flush()
            assert (await session_valuation(session, worker, **arguments))["reason"] == (
                "FUTURE_ACCOUNT_FILL"
            )
            trade.executed_at = now
            positions[0].marked_at = now - timedelta(minutes=1)
            await session.flush()
            assert (await session_valuation(session, worker, **arguments))["reason"] == (
                "POSITION_MARK_STALE_OR_UNAVAILABLE"
            )
    finally:
        await worker.stop()
        await client.aclose()
