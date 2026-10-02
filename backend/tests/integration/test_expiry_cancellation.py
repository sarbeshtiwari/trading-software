"""Real pending option orders cannot settle after unresolved expiry cancellation."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.brokers.paper.engine import FillConfig
from app.core.clock import IST
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.trading import Order, Position
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from app.security.auth import OwnerAuth
from tests.integration.test_option_evidence import credentials, setup_option_worker

__all__ = ["credentials"]


@pytest.mark.parametrize("failure", ["none", "worker", "monitor", "receipt"])
async def test_expiry_cancellation_before_settlement_and_recovery(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, failure
):
    worker, _provider, client, _chain = await setup_option_worker(
        credentials,
        fake_clock,
        tmp_path,
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000),
    )
    try:
        await worker.cycle()
        assert not worker.failed
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.status == OrderStatus.OPEN and order.filled_quantity == 0
            instrument = await session.get(Instrument, order.instrument_id)
            expiry = instrument.expiry_date
        fake_clock.set_to(datetime(expiry.year, expiry.month, expiry.day, 14, 30, tzinfo=IST))
        if failure in {"worker", "monitor"}:
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    worker.executor,
                    "cancel",
                    AsyncMock(side_effect=RuntimeError("boundary failure")),
                )
                settle = AsyncMock(wraps=worker.executor.broker.settle_open_orders)
                scoped.setattr(worker.executor.broker, "settle_open_orders", settle)
                worker.phase = "INTRADAY"
                with pytest.raises(SafetyError, match="CANCELLATION_UNRESOLVED"):
                    if failure == "worker":
                        await worker._work()
                    else:
                        await worker.executor.monitor_once()
                settle.assert_not_awaited()
                assert not worker.executor.gate.new_entries_allowed
        elif failure == "receipt":
            hygiene = PaperOrderHygiene(worker.executor)
            original = hygiene.record

            async def interrupted(order, chain, event, result):
                if event == "ENTRY_CANCEL_RESULT":
                    raise RuntimeError("receipt storage interruption")
                return await original(order, chain, event, result)

            monkeypatch.setattr(hygiene, "record", interrupted)
            with pytest.raises(RuntimeError, match="receipt storage interruption"):
                await hygiene.run()
        else:
            assert await PaperOrderHygiene(worker.executor).run()
        restored = PaperExecution(
            worker.executor.source, settings=worker.settings, clock=fake_clock
        )
        await restored.recover()
        assert await PaperOrderHygiene(restored).run()
        assert await PaperOrderHygiene(restored).run()
        async with db_session.session_scope() as session:
            current = await session.get(Order, order.id)
            assert current.status == OrderStatus.CANCELLED
            assert current.filled_quantity == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
            results = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "ENTRY_CANCEL_RESULT")
                )
            )
            assert results[-1].result["reason"] == "FNO_EXPIRY_ENTRY_CUTOFF"
        assert len(await restored.broker.list_orders()) == 1
        access, _ = await OwnerAuth().login("owner", credentials[1])
        client.headers["Authorization"] = f"Bearer {access}"
        response = await client.get("/api/v1/journal", params={"kind": "ORDER_ACTION"})
        assert response.status_code == 200, response.text
        entries = response.json()["entries"]
        cancelled = [entry for entry in entries if entry["outcome"] == "CANCELLED"]
        assert len(cancelled) == 1
        assert cancelled[0]["entry_order_id"] == order.id
        assert cancelled[0]["integrity"] == "AUDIT_BOUND"
        assert (
            cancelled[0]["indicator_snapshot"]["order_action"]["reason"]
            == "FNO_EXPIRY_ENTRY_CUTOFF"
        )
        assert cancelled[0]["net_pnl"] is None
    finally:
        await worker.stop()
        await client.aclose()
