"""Isolated provider fixtures verify actual worker coverage and costed PAPER evidence."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.portfolio.journal_integrity import journal_verified
from app.security.auth import OwnerAuth
from app.strategies.paper_policy import evidence_chain
from app.strategies.review import review_strategy
from app.trading.worker import PaperWorker
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials"]
PATH = "/api/v1/strategies/closed-candle-breakout/evidence"
POLICY = {
    "version": 1,
    "minimum_sessions": 1,
    "minimum_trades": 1,
    "minimum_session_coverage_fraction": "0.001",
    "sample_interval_seconds": 30,
    "maximum_sample_gap_seconds": 40,
}


async def declare(client, version=1):
    response = await client.post(
        PATH + "/policy",
        json={
            "version": "1",
            "policy": POLICY | {"version": version},
            "reason": "Isolated explicit coverage fixture",
        },
    )
    assert response.status_code == 200, response.text


async def finish_day(worker, provider, fake_clock, client, credentials):
    fake_clock.advance(timedelta(seconds=30))
    quote = provider.get_quote.return_value
    provider.get_quote.return_value = replace(
        quote,
        observed_at=fake_clock.now(),
        ltp=Decimal(108),
        bids=(replace(quote.bids[0], price=Decimal(108)),),
        asks=(replace(quote.asks[0], price=Decimal("108.05")),),
    )
    await worker.cycle()
    assert not worker.failed, worker.detail
    fake_clock.set_to(fake_clock.now().replace(hour=15, minute=30, second=0, microsecond=0))
    await worker.cycle()
    assert not worker.failed, worker.detail
    access, _ = await OwnerAuth().login("owner", credentials[1])
    client.headers["Authorization"] = f"Bearer {access}"


@pytest.mark.parametrize("origin", [DataOrigin.LIVE, DataOrigin.SYNTHETIC])
async def test_worker_session_costed_trade_review_and_policy_change(
    db_engine, credentials, fake_clock, tmp_path, origin, monkeypatch
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True, origin=origin
    )
    original_append = AuditService.append_in_session

    async def elapsed_audit(service, session, identity, details, **kwargs):
        if identity.event_type == "PAPER_COVERAGE_SAMPLE":
            fake_clock.advance(timedelta(microseconds=1))
        return await original_append(service, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", elapsed_audit)
    try:
        token = client.headers.pop("Authorization")
        assert (await client.get(PATH + "?version=1")).status_code == 401
        client.headers["Authorization"] = token
        await declare(client)
        refused = await client.post(
            PATH + "/policy",
            json={"version": "1", "policy": POLICY, "reason": "Repeated policy must be refused"},
        )
        assert refused.status_code == 409
        await worker.cycle()
        assert not worker.failed, worker.detail
        await finish_day(worker, provider, fake_clock, client, credentials)
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == "closed-candle-breakout"
                )
            )
            direct = await review_strategy(session, row)
        response = await client.get(PATH + "?version=1")
        assert response.status_code == 200, response.text
        evidence = response.json()
        assert evidence == direct.model_dump(mode="json")
        paper = evidence["paper"]
        expected = 1 if origin == DataOrigin.LIVE else 0
        assert paper["eligible_sessions"] == paper["eligible_trades"] == expected
        assert paper["threshold_passed"] is bool(expected)
        assert evidence["live_approved"] is False
        assert paper["net_pnl"] == ("856.56" if expected else None)
        assert paper["gross_pnl"] == ("888.00" if expected else None)
        assert paper["charges"] == ("31.44" if expected else None)
        assert Decimal(paper["sessions"][0]["observed_seconds"]) == (
            Decimal("30.000001") if expected else 0
        )
        reviewed = await client.post(
            PATH + "/review",
            json={
                "version": "1",
                "reason": "Review actual isolated worker lifecycle",
                "confirmation": "REVIEW STRATEGY EVIDENCE",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == "closed-candle-breakout"
                )
            )
            journal = await session.scalar(sa.select(JournalEntry))
            assert await journal_verified(session, journal.id)
            assert not row.live_approved and not row.enabled_live
            assert row.paper_sessions == row.paper_trades == expected
            assert await AuditService(fake_clock).verify(evidence_chain("sev", row.id))
        async with db_engine.begin() as connection:
            await connection.execute(
                sa.update(JournalEntry)
                .where(JournalEntry.id == journal.id)
                .values(net_pnl=journal.net_pnl + 1)
            )
        changed = (await client.get(PATH + "?version=1")).json()["paper"]
        assert changed["eligible_trades"] == 0
        assert "JOURNAL_AUDIT_UNAVAILABLE_OR_CHANGED" in changed["excluded_trades"].values()
        await declare(client, version=2)
        reset = (await client.get(PATH + "?version=1")).json()["paper"]
        assert reset["eligible_sessions"] == reset["eligible_trades"] == 0
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("age", [-1, 61])
async def test_invalid_quote_cannot_contribute_paper_coverage(
    db_engine, credentials, fake_clock, tmp_path, age
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True, origin=DataOrigin.LIVE
    )
    try:
        await declare(client)
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now() - timedelta(seconds=age)
        )
        await worker.cycle()
        assert worker.failed
        response = await client.get(PATH + "?version=1")
        assert response.status_code == 200, response.text
        paper = response.json()["paper"]
        assert paper["eligible_sessions"] == paper["eligible_trades"] == 0
        assert Decimal(paper["sessions"][0]["observed_seconds"]) == 0
    finally:
        await worker.stop()
        await client.aclose()


async def test_restart_does_not_infer_coverage_and_tampering_blocks_review(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, costed=True, origin=DataOrigin.LIVE
    )
    try:
        await declare(client)
        await worker.cycle()
        await worker.stop()
        worker = PaperWorker(
            PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock),
            provider=provider,
            calendar=worker.calendar,
            lock_path=tmp_path / "reference-worker.lock",
        )
        await worker.start(schedule=False)
        await finish_day(worker, provider, fake_clock, client, credentials)
        paper = (await client.get(PATH + "?version=1")).json()["paper"]
        assert paper["eligible_sessions"] == paper["eligible_trades"] == 0
        assert Decimal(paper["sessions"][0]["observed_seconds"]) == 0
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "PAPER_COVERAGE_SAMPLE")
            )
            event.result = event.result | {"healthy": not event.result["healthy"]}
        assert (await client.get(PATH + "?version=1")).status_code == 409
    finally:
        await worker.stop()
        await client.aclose()


async def test_journal_audit_failure_rolls_back_local_close_and_recovers_once(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    original = AuditService.append_in_session

    async def fail_journal(service, session, identity, details, **kwargs):
        if identity.event_type == "PAPER_JOURNAL_RECORDED":
            raise RuntimeError("isolated journal audit failure")
        return await original(service, session, identity, details, **kwargs)

    try:
        await worker.cycle()
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        with monkeypatch.context() as context:
            context.setattr(AuditService, "append_in_session", fail_journal)
            await worker.cycle()
        assert worker.failed
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(JournalEntry)) is None
            assert (await session.scalar(sa.select(Position))).net_quantity == 111
        await worker.stop()
        restored = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
        await restored.recover()
        await restored.recover()
        async with db_session.session_scope() as session:
            journals = list((await session.scalars(sa.select(JournalEntry))).all())
            assert len(journals) == 1 and journals[0].net_pnl == Decimal("856.56")
            assert await journal_verified(session, journals[0].id)
            assert (await session.scalar(sa.select(Position))).net_quantity == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
    finally:
        await worker.stop()
        await client.aclose()
