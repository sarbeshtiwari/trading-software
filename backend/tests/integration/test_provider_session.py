"""Session cutoff and recovery through the actual provider-driven PAPER worker."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import ExitReason, OrderStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.system import Heartbeat
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.monitoring.gate import get_trading_gate, reset_trading_gate
from app.trading.worker import PaperWorker, _runtime
from tests.integration.test_contract_production import publish_policy
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.integration.test_regime_sources import configure_sources

__all__ = ["credentials"]


@pytest.mark.parametrize("outage", [False, True])
async def test_provider_trade_squareoff_and_eod_recovery(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    outage,
    monkeypatch,
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await configure_sources(worker, provider, fake_clock)
        await publish_policy(worker, provider, fake_clock)
        quotes = provider.get_ohlc.return_value
        down = next(key for key in quotes if "down" in key)
        quotes[down] = replace(quotes[down], close=Decimal(101))
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert len(await worker.executor.broker.list_orders()) == 1
        calls = provider.get_option_chain.await_count
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=10, second=0, microsecond=0))
        quote = provider.get_quote.return_value
        if not outage:
            provider.get_quote.return_value = replace(
                quote,
                observed_at=fake_clock.now(),
                ltp=Decimal(102),
                bids=(replace(quote.bids[0], price=Decimal(102)),),
                asks=(replace(quote.asks[0], price=Decimal("102.05")),),
            )
        await worker.cycle()
        assert worker.phase == "EXIT_WINDOW"
        assert provider.get_option_chain.await_count == calls
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            exit_order = await session.scalar(sa.select(Order).where(Order.role == "EXIT"))
            assert exit_order.exit_reason == ExitReason.SQUARE_OFF
            assert position.net_quantity == (111 if outage else 0)
            assert exit_order.status == (OrderStatus.OPEN if outage else OrderStatus.EXECUTED)
            assert await session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == (
                0 if outage else 1
            )
        if outage:
            assert worker.failed
        else:
            assert not worker.failed, worker.detail
        await worker.stop()
        reset_trading_gate()
        get_trading_gate().clear("startup")
        fake_clock.set_to(fake_clock.now().replace(hour=15, minute=30))
        provider.get_quote.return_value = replace(
            quote,
            observed_at=fake_clock.now(),
            ltp=Decimal(102),
            bids=(replace(quote.bids[0], price=Decimal(102)),),
            asks=(replace(quote.asks[0], price=Decimal("102.05")),),
        )
        executor = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
        worker = PaperWorker(
            executor, provider=provider, calendar=worker.calendar, lock_path=worker.lock.path
        )
        monkeypatch.setattr(_runtime, "worker", worker)
        await worker.start(schedule=False)
        await worker.cycle()
        assert worker.phase == "EOD_RECONCILIATION"
        assert worker.failed == outage
        async with db_session.session_scope() as session:
            assert (await session.scalar(sa.select(Position))).net_quantity == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.gross_pnl == Decimal(222)
            assert (
                journal.charges is not None
                and journal.net_pnl == journal.gross_pnl - journal.charges
            )
            assert (await session.get(Heartbeat, "paper-worker")).detail["failed"] == outage
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": credentials[1]},
            headers={"X-Requested-With": "ATS"},
        )
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200, response.text
        state = response.json()
        assert len(state["journal"]) == 1
        scheduler = next(item for item in state["components"] if item["name"] == "scheduler")
        assert scheduler["status"] == ("DEGRADED" if outage else "EOD_RECONCILIATION")
        assert scheduler["detail"] == worker.detail
        assert "EOD_RECONCILIATION: 0 open MIS positions; 0 unresolved orders" in worker.detail
        async with db_session.session_scope() as session:
            transitions = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(
                            AuditEvent.event_type == "PAPER_SESSION_TRANSITION",
                        )
                        .order_by(AuditEvent.occurred_at)
                    )
                ).all()
            )
            assert [row.result["phase"] for row in transitions] == [
                "INTRADAY",
                "EXIT_WINDOW",
                "EOD_RECONCILIATION",
            ]
        await worker.cycle()
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.event_type == "PAPER_SESSION_TRANSITION",
                    )
                )
                == 3
            )
        fake_clock.set_to((fake_clock.now() + timedelta(days=1)).replace(hour=10, minute=0))
        while fake_clock.now().weekday() >= 5:
            fake_clock.advance(timedelta(days=1))
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value, observed_at=fake_clock.now()
        )
        await worker.cycle()
        assert worker.phase == "INTRADAY"
        assert worker.failed == outage
        assert len(await worker.executor.broker.list_orders()) == 2
        if outage:
            assert "PAPER_WORKER_REVIEW_REQUIRED" in get_trading_gate().reason()
    finally:
        await worker.stop()
        await client.aclose()
