"""A full sampled session uses recorded fixtures, never fabricated production inputs."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.backtest.bootstrap import HistoricalManifest, prepare_run
from app.backtest.engine import run_prepared
from app.config import get_settings
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, OptionType, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult
from app.db.models.trading import Order, Position
from app.marketdata.models import (
    Bar,
    Greeks,
    InstrumentRef,
    OHLCQuote,
    OptionChain,
    OptionLeg,
    OptionStrike,
)
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.recordings import (
    CandleRecording,
    RecordingBundle,
    read_recording,
    snapshot_record,
    write_recording,
)
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["isolated_database"]


def full_session():
    short, request = recorded_trade()
    opened = request.start_at.replace(hour=9, minute=15)
    end = opened.replace(hour=15, minute=40)
    expiry = opened.date() + timedelta(days=2)
    equity, index = short.candles
    candles = []
    for series in (equity, index):
        bars = []
        for offset in range(375):
            price = (
                Decimal(1000) + Decimal("0.05") * offset
                if series is index
                else Decimal(98 if offset < 44 else 100)
            )
            bars.append(
                Bar(opened + timedelta(minutes=offset), price, price + 1, price - 1, price, 1000)
            )
        if series is equity:
            bars[44] = replace(bars[44], open=Decimal(98), high=Decimal(100), low=Decimal(98))
        candles.append(
            CandleRecording(
                source="full-session deterministic fixture",
                original_data_origin=DataOrigin.SYNTHETIC,
                availability_model="NOMINAL_BAR_CLOSE",
                instrument=series.instrument,
                interval_minutes=1,
                bars=bars,
            )
        )
    rows = []
    quote = short.snapshots[0].value
    breadth_ids = ("breadth-up-one", "breadth-up-two", "breadth-flat")
    for offset in range(386):
        moment = opened + timedelta(minutes=offset)
        price = Decimal(98 if offset < 45 else 108 if offset == 46 else 100)
        values = [
            replace(
                quote,
                observed_at=moment,
                ltp=price,
                bids=(
                    replace(
                        quote.bids[0], price=price if offset == 46 else price - Decimal("0.05")
                    ),
                ),
                asks=(replace(quote.asks[0], price=price),),
            )
        ]
        for identifier in breadth_ids:
            values.append(
                OHLCQuote(
                    InstrumentRef(identifier, Exchange.NSE, Segment.CASH),
                    Decimal(100),
                    Decimal(101),
                    Decimal(99),
                    Decimal(100 if identifier == "breadth-flat" else 101),
                    moment,
                    previous_close=Decimal(100),
                    data_origin=DataOrigin.SYNTHETIC,
                )
            )
        values.append(
            OptionChain(
                underlying="INDEX-FIXTURE",
                expiry=expiry,
                observed_at=moment,
                spot=Decimal(1000),
                data_origin=DataOrigin.SYNTHETIC,
                strikes=(
                    OptionStrike(
                        Decimal(1000),
                        OptionLeg(
                            "fixture-call",
                            OptionType.CE,
                            greeks=Greeks(implied_volatility=Decimal(18), computed_at=moment),
                        ),
                        OptionLeg(
                            "fixture-put",
                            OptionType.PE,
                            greeks=Greeks(implied_volatility=Decimal(22), computed_at=moment),
                        ),
                    ),
                ),
            )
        )
        rows.extend(
            snapshot_record(RecordedSnapshot("full-session fixture", moment, value))
            for value in values
        )
    recording = RecordingBundle(
        format_version=1,
        source="full-session deterministic fixture",
        candles=candles,
        snapshots=rows,
    )
    payload = request.model_dump()
    payload.update(start_at=opened, end_at=end, recording_sha256=recording.content_hash())
    source = payload["reference_inputs"]
    source["costs"] = None
    source["contract_source"] = {
        "instrument_id": "first",
        "data_origin": "REPLAY",
        "source": "fixture owner policy",
        "known_at": opened,
        "valid_from": opened,
        "valid_until": end + timedelta(minutes=1),
        "risk_cost_reserve_per_unit": "0.5",
    }
    regime = source["regime_source"]
    regime.update(
        implied_volatility=None,
        breadth=None,
        provider_source={
            "source": "fixture constituent declaration",
            "known_at": opened,
            "valid_from": opened,
            "valid_until": end + timedelta(minutes=1),
            "breadth_instrument_ids": breadth_ids,
            "option_underlying": "INDEX-FIXTURE",
            "option_expiry": expiry,
        },
    )
    payload["instruments"] = list(payload["instruments"])
    for identifier in breadth_ids:
        payload["instruments"].append(
            payload["instruments"][0] | {"id": identifier, "trading_symbol": identifier}
        )
    return recording, HistoricalManifest.model_validate(payload)


@pytest.mark.parametrize("missing_quote", [False, True])
async def test_complete_sampled_session_from_open_through_eod(
    isolated_database, fake_clock, tmp_path, missing_quote
):
    recording, request = full_session()
    if missing_quote:
        gap = request.start_at + timedelta(minutes=35)
        recording = recording.model_copy(
            update={
                "snapshots": tuple(
                    row
                    for row in recording.snapshots
                    if not (row.kind == "QUOTE" and row.available_at == gap)
                ),
            }
        )
        request = request.model_copy(update={"recording_sha256": recording.content_hash()})
    recording_path = tmp_path / "full-session.json"
    write_recording(recording_path, recording)
    recording = read_recording(recording_path)
    fake_clock.set_to(request.start_at)
    settings = get_settings().model_copy(
        update={
            "starting_capital": Decimal(100000),
            "paper_cycle_seconds": 60,
            "entry_blackout_open_minutes": 30,
        }
    )
    worker = await prepare_run(
        request, recording, settings=settings, clock=fake_clock, lock_path=tmp_path / "session.lock"
    )
    assert await run_prepared(worker, request) == ("FAILED" if missing_quote else "COMPLETED"), (
        worker.detail
    )
    async with db_session.session_scope() as session:
        if missing_quote:
            assert worker.failed
            assert fake_clock.now() == request.start_at + timedelta(minutes=35)
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "MARKET_INPUT_REJECTED")
                )
                == 1
            )
            assert (await session.scalar(sa.select(BacktestResult))).trade_count == 0
            result = await session.scalar(sa.select(BacktestResult))
            assert result.equity_curve[-1][1] is None
            return
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
        assert (await session.scalar(sa.select(Position))).net_quantity == 0
        result = await session.scalar(sa.select(BacktestResult))
        assert result.trade_count == 1
        assert result.gross_pnl == Decimal(888)
        assert result.total_charges == Decimal("31.44")
        assert result.net_pnl == Decimal("856.56")
        assert len(result.equity_curve) == 387
        assert Decimal(result.equity_curve[0][1]) == 100000
        assert Decimal(result.equity_curve[-1][1]) == Decimal("100856.56")
        assert len(result.window_parameters["trade_links"]) == 1
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "REFERENCE_STAND_DOWN")
            )
            == 0
        )
        events = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.event_type == "PAPER_SESSION_TRANSITION")
                    .order_by(AuditEvent.occurred_at)
                )
            ).all()
        )
        assert [event.result["phase"] for event in events] == [
            "MARKET_OPEN",
            "INTRADAY",
            "NO_ENTRY_WINDOW",
            "EXIT_WINDOW",
            "EOD_RECONCILIATION",
            "MARKET_CLOSE",
        ]
        assert "0 unresolved orders" in worker.detail
