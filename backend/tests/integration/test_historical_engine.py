"""Recorded prices drive the shared worker in an exclusively prepared database."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.backtest.bootstrap import HistoricalManifest, prepare_run
from app.backtest.engine import run_prepared
from app.backtest.storage import sample_account, stored_samples
from app.config import get_settings
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.db import session as db_session
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.marketdata.models import Bar, InstrumentRef
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.recordings import CandleRecording, RecordingBundle, snapshot_record
from app.monitoring.gate import get_trading_gate
from tests.integration.test_historical_bootstrap import isolated_database, manifest
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_option_chain import OBSERVED

__all__ = ["isolated_database"]


def recorded_trade():
    fixture = fixture_provider()
    reference = InstrumentRef("FIRST", Exchange.NSE, Segment.CASH)
    quote = replace(fixture.get_quote.return_value, instrument=reference)
    index = InstrumentRef("INDEX-FIXTURE", Exchange.NSE, Segment.CASH)
    index_bars = tuple(
        Bar(
            OBSERVED - timedelta(minutes=29 - offset),
            Decimal(1000) + Decimal("0.05") * offset,
            Decimal(1001) + Decimal("0.05") * offset,
            Decimal(999) + Decimal("0.05") * offset,
            Decimal(1000) + Decimal("0.05") * offset,
            1000,
        )
        for offset in range(29)
    )
    exit_quote = replace(
        quote,
        observed_at=OBSERVED + timedelta(seconds=5),
        ltp=Decimal(108),
        bids=(replace(quote.bids[0], price=Decimal(108)),),
        asks=(replace(quote.asks[0], price=Decimal("108.05")),),
    )
    recording = RecordingBundle(
        format_version=1,
        source="isolated engine fixture",
        candles=[
            CandleRecording(
                source="isolated engine fixture",
                original_data_origin=DataOrigin.SYNTHETIC,
                availability_model="NOMINAL_BAR_CLOSE",
                instrument=instrument,
                interval_minutes=1,
                bars=bars,
            )
            for instrument, bars in (
                (reference, fixture.get_candles.return_value),
                (index, index_bars),
            )
        ],
        snapshots=[
            snapshot_record(RecordedSnapshot("isolated engine fixture", value.observed_at, value))
            for value in (quote, exit_quote)
        ],
    )
    payload = manifest(recording).model_dump()
    payload["start_at"] = OBSERVED
    payload["end_at"] = OBSERVED + timedelta(seconds=5)
    costs = payload["reference_inputs"]["costs"]
    costs.update(observed_at=OBSERVED, available_at=OBSERVED)
    regime = payload["reference_inputs"]["regime_source"]
    for name in ("implied_volatility", "breadth"):
        regime[name].update(observed_at=OBSERVED, available_at=OBSERVED)
    regime["calendar"].update(coverage_end=OBSERVED + timedelta(days=1))
    payload["fees"][0].update(effective_to=OBSERVED + timedelta(days=1))
    return recording, HistoricalManifest.model_validate(payload)


@pytest.mark.parametrize("seconds,expected", [(5, "COMPLETED"), (4, "INCOMPLETE")])
async def test_recorded_window_runs_real_worker_without_future_exit(
    isolated_database, fake_clock, tmp_path, seconds, expected
):
    recording, request = recorded_trade()
    request = request.model_copy(update={"end_at": OBSERVED + timedelta(seconds=seconds)})
    fake_clock.set_to(OBSERVED)
    settings = get_settings().model_copy(update={"starting_capital": Decimal(100000)})
    worker = await prepare_run(
        request, recording, settings=settings, clock=fake_clock, lock_path=tmp_path / "run.lock"
    )
    assert await run_prepared(worker, request) == expected, worker.detail
    assert fake_clock.now() == request.end_at
    assert not worker.running
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        result = await session.scalar(sa.select(BacktestResult))
        assert row.status == expected and row.progress_pct == 100
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == (
            2 if seconds == 5 else 1
        )
        position = await session.scalar(sa.select(Position))
        samples = result.window_parameters["account_samples"]
        assert samples[0]["baseline"] and Decimal(samples[0]["net_equity"]) == 100000
        assert Decimal(samples[1]["net_equity"]) == Decimal("99986.16")
        assert Decimal(samples[1]["gross_exposure"]) == 11100
        assert samples[1]["position_ids"] == [position.id]
        assert all(sample["audit_event_id"] for sample in samples)
        if seconds == 5:
            assert position.net_quantity == 0
            assert result.trade_count == 1
            assert result.gross_pnl == Decimal(888)
            assert result.total_charges == Decimal("31.44")
            assert result.net_pnl == Decimal("856.56")
            assert result.total_return == Decimal("0.008566")
            assert result.max_drawdown == Decimal("0.000138")
            metrics = result.window_parameters["descriptive_metrics"]
            assert Decimal(metrics["total_return"]) == Decimal("0.0085656")
            assert Decimal(metrics["max_drawdown"]) == Decimal("0.0001384")
            assert result.win_rate == 1 and result.profit_factor is None
            assert result.expectancy == Decimal("856.56")
            assert result.sharpe is None and result.sortino is None and result.cagr is None
            assert result.avg_holding_seconds == 5
            assert Decimal(result.equity_curve[-1][1]) == Decimal("100856.56")
            trade = await session.scalar(sa.select(BacktestTrade))
            journal = await session.scalar(sa.select(JournalEntry))
            assert trade.net_pnl == journal.net_pnl
            assert trade.quantity == 111 and trade.entry_price == 100 and trade.exit_price == 108
            assert result.window_parameters["trade_links"][0]["journal_id"] == journal.id
            assert result.window_parameters["trade_links"][0]["trade_id"] == trade.id
        else:
            assert position.net_quantity == 111
            assert result.trade_count == 0
            assert worker.reference_runtime.provider.next_event_at == OBSERVED + timedelta(
                seconds=5
            )
    with pytest.raises(ValueError):
        await run_prepared(worker, request)
    if seconds == 4:
        fake_clock.advance_seconds(20)
        async with db_session.session_scope() as session:
            fill = await session.scalar(sa.select(Trade))
            fill.cost_breakdown = {"status": "UNAVAILABLE"}
            await session.flush()
            await sample_account(session, worker, "unavailable-observation")
        async with db_session.session_scope() as session:
            sample = (await stored_samples(session, "unavailable-observation"))[0]
            assert sample["status"] == "UNAVAILABLE"
            assert sample["net_equity"] is None and sample["gross_exposure"] is None
            assert sample["charges"] is None
            assert set(sample["reasons"]) == {
                "FILL_COSTS_UNAVAILABLE",
                "POSITION_MARK_UNAVAILABLE_OR_STALE",
            }


async def test_execution_settings_change_is_rejected_before_start(
    isolated_database, fake_clock, tmp_path
):
    recording, request = recorded_trade()
    fake_clock.set_to(OBSERVED)
    settings = get_settings().model_copy(update={"starting_capital": Decimal(100000)})
    worker = await prepare_run(
        request, recording, settings=settings, clock=fake_clock, lock_path=tmp_path / "run.lock"
    )
    settings.paper_cycle_seconds += 1
    with pytest.raises(ValueError, match="configuration changed"):
        await run_prepared(worker, request)
    async with db_session.session_scope() as session:
        assert (await session.get(BacktestRun, request.run_id)).status == "PREPARED"
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_historical_start_does_not_clear_unrelated_safety_blocker(
    isolated_database, fake_clock, tmp_path
):
    recording, request = recorded_trade()
    fake_clock.set_to(OBSERVED)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "run.lock",
    )
    get_trading_gate().block("owner_review", "REVIEW_REQUIRED")
    await run_prepared(worker, request)
    assert "owner_review: REVIEW_REQUIRED" in get_trading_gate().state.reasons
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        assert (await session.scalar(sa.select(BacktestResult))).trade_count == 0


async def test_unexpected_runner_failure_is_persisted_and_worker_stops(
    isolated_database, fake_clock, tmp_path, monkeypatch
):
    recording, request = recorded_trade()
    fake_clock.set_to(OBSERVED)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "run.lock",
    )

    async def unavailable():
        raise RuntimeError("private failure details")

    monkeypatch.setattr(worker, "cycle", unavailable)
    with pytest.raises(RuntimeError, match="private failure"):
        await run_prepared(worker, request)
    assert not worker.running
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        assert row.status == "FAILED" and row.error_detail == "RuntimeError"
        assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestResult)) == 0
