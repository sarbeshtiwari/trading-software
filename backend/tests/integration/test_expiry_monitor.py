from datetime import datetime
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.clock import IST
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.instrument import Instrument
from app.db.models.trading import Position
from tests.integration.test_option_evidence import credentials, setup_option_worker

__all__ = ["credentials"]


async def test_actual_option_position_has_durable_expiry_alert(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle()
        assert not worker.failed
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            instrument = await session.get(Instrument, position.instrument_id)
            expiry = instrument.expiry_date
            quantity = position.net_quantity
        fake_clock.set_to(datetime(expiry.year, expiry.month, expiry.day, 14, 30, tzinfo=IST))
        for _attempt in range(2):
            with pytest.raises(SafetyError):
                await worker.executor.monitor_once()
        async with db_session.session_scope() as session:
            events = list(
                await session.scalars(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "FNO_EXPIRY_ACTION_REQUIRED"
                    )
                )
            )
            assert len(events) == 1
            assert events[0].position_id == position.id
            assert events[0].result["quantity"] == quantity
            notices = list(
                await session.scalars(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "NOTIFICATION_REQUESTED")
                )
            )
            assert (
                sum(
                    event.result["notification"]["event_type"] == "FNO_EXPIRY_ACTION_REQUIRED"
                    for event in notices
                )
                == 1
            )
    finally:
        await worker.stop()
        await client.aclose()


async def test_expiry_cutoff_vetoes_existing_approval(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, client, _chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    submit = AsyncMock(wraps=worker.executor.broker.place_order)
    monkeypatch.setattr(worker.executor.broker, "place_order", submit)
    try:
        await worker.reference_runtime.cycle()
        async with db_session.session_scope() as session:
            proposal = await session.scalar(
                sa.select(Proposal).where(Proposal.strategy_id == "long-option-breakout")
            )
            instrument = await session.get(Instrument, proposal.instrument_id)
            instrument.expiry_date = fake_clock.now().date()
        worker.settings.fno_expiry_entry_cutoff_time = fake_clock.now().strftime("%H:%M")
        with pytest.raises(SafetyError, match="FNO_EXPIRY_ENTRY_CUTOFF"):
            await worker.executor.submit(proposal.id)
        submit.assert_not_awaited()
    finally:
        await worker.stop()
        await client.aclose()
