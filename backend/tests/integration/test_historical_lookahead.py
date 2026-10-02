"""Adversarial strategy code runs through the real provider and decision services."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.backtest.bootstrap import prepare_run
from app.backtest.engine import run_prepared
from app.config import get_settings
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult
from app.db.models.trading import Order
from app.marketdata.recordings import RecordingBundle
from app.strategies.reference import ClosedCandleBreakout
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["isolated_database"]


@pytest.mark.parametrize("attempt", ["next_bar", "undeclared_input", "future_signal"])
async def test_deliberate_lookahead_is_audited_without_an_order(
    isolated_database,
    fake_clock,
    tmp_path,
    monkeypatch,
    attempt,
):
    recording, manifest = recorded_trade()
    series = recording.candles[0]
    future = replace(
        series.bars[-1],
        ts=manifest.end_at + timedelta(minutes=1),
        open=Decimal(900),
        high=Decimal(1000),
        low=Decimal(800),
        close=Decimal(950),
    )
    recording = RecordingBundle.model_validate(
        recording.model_copy(
            update={
                "candles": (
                    series.model_copy(update={"bars": (*series.bars, future)}),
                    *recording.candles[1:],
                ),
            }
        ).model_dump()
    )
    manifest = manifest.model_copy(update={"recording_sha256": recording.content_hash()})
    attempts = []
    original_entry = ClosedCandleBreakout.entry

    def cheating_entry(strategy, context):
        bars = context.candles
        assert all(bar.ts + timedelta(minutes=1) <= strategy.clock.now() for bar in bars)
        assert future not in bars
        attempts.append(strategy.clock.now())
        if attempt == "next_bar":
            return bars[len(bars)]
        if attempt == "undeclared_input":
            return context.future_candles
        signal = original_entry(strategy, context)
        assert signal is not None
        return signal.model_copy(
            update={"generated_at": strategy.clock.now() + timedelta(minutes=1)}
        )

    monkeypatch.setattr(ClosedCandleBreakout, "entry", cheating_entry)
    fake_clock.set_to(manifest.start_at)
    worker = await prepare_run(
        manifest,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "lookahead.lock",
    )
    outcome = await run_prepared(worker, manifest)
    assert attempts == [manifest.start_at]
    assert not worker.failed, worker.detail
    assert outcome == "INCOMPLETE"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        result = await session.scalar(sa.select(BacktestResult))
        assert result.trade_count == 0
        assert "1 invalid strategy evaluations" in result.notes
        evaluation = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.event_type == "REFERENCE_EVALUATION",
            )
        )
        assert evaluation.result["strategy_reason"] == (
            "STRATEGY_DATA_BOUNDARY_VIOLATION"
            if attempt == "next_bar"
            else "INVALID_OR_UNAVAILABLE_STRATEGY_INPUT_OUTPUT"
        )
