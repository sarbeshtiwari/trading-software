"""Cancellation journals commit with their outcome, without fictional closed trades."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position
from app.execution.hygiene import PaperOrderHygiene
from app.notifications.order_hygiene import order_hygiene_snapshot
from app.portfolio.journal_integrity import journal_verified
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def test_cancellation_journal_atomic_recovery_and_api(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market["quantities"] = [10000, 100]
    identifier = await engine.submit(proposal)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                "app.journal.order_actions.bind_journal",
                AsyncMock(side_effect=RuntimeError("isolated seal failure")),
            )
            with pytest.raises(RuntimeError, match="seal failure"):
                await PaperOrderHygiene(engine).run(all_pending=True)
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 0
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
                )
                == 0
            )
        assert await PaperOrderHygiene(engine).run()
        assert await PaperOrderHygiene(engine).run()
        response = await client.get("/api/v1/journal", params={"kind": "ORDER_ACTION"})
        assert response.status_code == 200, response.text
        entries = response.json()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["entry_order_id"] == identifier
        assert row["integrity"] == "AUDIT_BOUND"
        assert row["outcome"] == "CANCELLED"
        assert all(
            row[field] is None
            for field in ("gross_pnl", "charges", "net_pnl", "closed_at", "actual_exit")
        )
        assert row["indicator_snapshot"]["order_action"]["filled_quantity"] == 100
        response = await client.get(f"/api/v1/journal/{row['id']}")
        assert response.status_code == 200, response.text
        async with db_session.session_scope() as session:
            assert await journal_verified(session, row["id"])
            assert (await session.scalar(sa.select(Position))).net_quantity == 100
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(JournalEntry)
                    .where(JournalEntry.kind == "TRADE")
                )
                == 0
            )
        assert len(await engine.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            observed = await order_hygiene_snapshot(
                session, fake_clock.utcnow() - timedelta(minutes=1), fake_clock.utcnow()
            )
            assert observed["cancellations"][0]["status"] == "CANCELLED"
            assert observed["cancellations"][0]["filled_quantity"] == 100
            earlier = await order_hygiene_snapshot(
                session,
                fake_clock.utcnow() - timedelta(minutes=2),
                fake_clock.utcnow() - timedelta(seconds=1),
            )
            assert earlier["cancellations"] == []
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_INTENT")
            )
            event.result = {"reason": "ALTERED"}
        async with db_session.session_scope() as session:
            with pytest.raises(ValueError, match="integrity"):
                await order_hygiene_snapshot(
                    session, fake_clock.utcnow() - timedelta(minutes=1), fake_clock.utcnow()
                )
    finally:
        await client.aclose()
