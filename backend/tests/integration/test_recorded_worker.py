"""Recorded deterministic fixtures use the real replay provider and PAPER services."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, OrderStatus, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order
from app.execution.paper import PaperExecution
from app.marketdata.models import InstrumentRef
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.recordings import (
    CandleRecording,
    RecordingBundle,
    read_recording,
    snapshot_record,
    write_recording,
)
from app.trading.inputs import ReferenceInputStore
from app.trading.worker import PaperWorker
from tests.integration.test_contract_production import publish_policy
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.integration.test_regime_sources import configure_sources

__all__ = ["credentials"]


async def test_recorded_snapshots_drive_real_strategy_risk_oms_and_net_journal(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
):
    worker, fixture, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await configure_sources(worker, fixture, fake_clock)
        await publish_policy(worker, fixture, fake_clock)
        now = fake_clock.now()
        source = await ReferenceInputStore(fake_clock).at("ins-test", DataOrigin.SYNTHETIC, now)
        await ReferenceInputStore(fake_clock).publish(
            source.model_copy(
                update={
                    "contract_source": source.contract_source.model_copy(
                        update={"data_origin": DataOrigin.REPLAY}
                    ),
                }
            ),
            actor="isolated-replay-owner",
        )
        series = []
        quote = fixture.get_quote.return_value
        index_ref = InstrumentRef("INDEX-FIXTURE", Exchange.NSE, Segment.CASH)
        for reference in (quote.instrument, index_ref):
            series.append(
                CandleRecording(
                    source="isolated-recording",
                    original_data_origin=DataOrigin.SYNTHETIC,
                    availability_model="NOMINAL_BAR_CLOSE",
                    instrument=reference,
                    interval_minutes=1,
                    bars=await fixture.get_candles(reference, 1, now - timedelta(hours=1), now),
                )
            )
        ohlc = fixture.get_ohlc.return_value
        down = next(key for key in ohlc if "down" in key)
        ohlc[down] = replace(ohlc[down], close=Decimal(101))
        snapshots = [
            *(
                RecordedSnapshot("isolated-recording", now, value)
                for value in (
                    quote,
                    *ohlc.values(),
                    fixture.get_option_chain.return_value,
                )
            ),
            RecordedSnapshot(
                "isolated-recording",
                now + timedelta(seconds=5),
                replace(
                    quote,
                    observed_at=now + timedelta(seconds=5),
                    ltp=Decimal(108),
                    bids=(replace(quote.bids[0], price=Decimal(108)),),
                    asks=(replace(quote.asks[0], price=Decimal("108.05")),),
                ),
            ),
        ]
        bundle = RecordingBundle(
            format_version=1,
            source="isolated-recording",
            candles=series,
            snapshots=[snapshot_record(row) for row in snapshots],
        )
        path = tmp_path / "session-recording.json"
        digest = write_recording(path, bundle)
        replay = read_recording(path).provider(clock=fake_clock)
        await worker.stop()
        while await replay.step() != now:
            assert not replay.exhausted
        executor = PaperExecution(replay.get_quote, settings=worker.settings, clock=fake_clock)
        worker = PaperWorker(
            executor, provider=replay, calendar=worker.calendar, lock_path=worker.lock.path
        )
        await worker.start(schedule=False)
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert worker.reference_runtime.detail == "RISK_APPROVED", worker.reference_runtime.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
            pending = await session.scalar(sa.select(Order))
            assert pending.status == OrderStatus.OPEN and pending.filled_quantity == 0
        assert executor.broker.next_execution_at == now + timedelta(milliseconds=150)
        fake_clock.set_to(executor.broker.next_execution_at)
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            entered = await session.scalar(sa.select(Order))
            assert entered.status == OrderStatus.EXECUTED and entered.filled_quantity == 111
        await replay.step()
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert executor.broker.next_execution_at == now + timedelta(seconds=5, milliseconds=150)
        fake_clock.set_to(executor.broker.next_execution_at)
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.gross_pnl == Decimal(888)
            assert journal.charges == Decimal("31.44")
            assert journal.net_pnl == Decimal("856.56")
            market = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.event_type == "REFERENCE_MARKET_SNAPSHOT",
                )
            )
            recording = market.data_used["recording_evidence"]
            assert len(recording) == 5
            assert all(item["source"] == "isolated-recording" for item in recording)
            assert all(item["recording_sha256"] == digest for item in recording)
            assert all(item["original_data_origin"] == "SYNTHETIC" for item in recording)
            assert all(
                datetime.fromisoformat(item["available_at"].replace("Z", "+00:00")) == now
                for item in recording
            )
            assert (
                journal.indicator_snapshot["decision_context"]["market"]["data_origin"] == "REPLAY"
            )
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert len(response.json()["journal"]) == 1
    finally:
        await worker.stop()
        await client.aclose()
