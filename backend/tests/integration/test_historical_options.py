"""Synthetic recordings exercise the isolated runner, not a parallel option simulator."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backtest.bootstrap import HistoricalManifest, prepare_run
from app.backtest.catalog import load_catalog
from app.backtest.engine import run_prepared
from app.backtest.launcher import launch_async
from app.backtest.reproducibility import fingerprints
from app.backtest.wf_inputs import freeze_experiment
from app.config import get_settings
from app.core.clock import IST
from app.core.data_origin import DataOrigin
from app.core.enums import GreekSource, InstrumentType, OptionType, Segment
from app.db import session as db_session
from app.db.base import Base
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position
from app.marketdata.models import Greeks, OptionChain, OptionLeg, OptionStrike
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.recordings import RecordingBundle, snapshot_record, write_recording
from app.trading.contracts import LongOptionContractSource
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade
from tests.integration.test_walkforward import experiment_files

__all__ = ["isolated_database"]


def recorded_option():
    recording, original = recorded_trade()
    now = original.start_at
    expiry = now.astimezone(IST).date() + timedelta(days=7)
    reference = replace(recording.candles[0].instrument, segment=Segment.FNO)
    candles = (
        recording.candles[0].model_copy(update={"instrument": reference}),
        *recording.candles[1:],
    )
    entry = replace(recording.snapshots[0].value, instrument=reference)
    exit_quote = replace(
        recording.snapshots[1].value,
        instrument=reference,
        ltp=Decimal(96),
        bids=(replace(entry.bids[0], price=Decimal(96)),),
        asks=(replace(entry.asks[0], price=Decimal("96.05")),),
    )
    greeks = Greeks(
        delta=Decimal(".5"),
        gamma=Decimal(".01"),
        theta=Decimal("-.1"),
        vega=Decimal(".2"),
        rho=Decimal(".01"),
        implied_volatility=Decimal(20),
        source=GreekSource.BROKER,
        computed_at=now,
    )
    chain = OptionChain(
        "TEST",
        expiry,
        now,
        strikes=(
            OptionStrike(
                Decimal(1000),
                call=OptionLeg(
                    "FIRST",
                    OptionType.CE,
                    ltp=Decimal(100),
                    bid=Decimal(99),
                    ask=Decimal(100),
                    greeks=greeks,
                ),
            ),
        ),
        data_origin=DataOrigin.SYNTHETIC,
    )
    recording = RecordingBundle(
        format_version=1,
        source="synthetic option replay fixture",
        candles=candles,
        snapshots=tuple(
            snapshot_record(
                RecordedSnapshot(
                    "synthetic option replay fixture",
                    value.observed_at,
                    value,
                )
            )
            for value in (entry, chain, exit_quote)
        ),
    )
    payload = original.model_dump()
    payload["recording_sha256"] = recording.content_hash()
    payload["instruments"][0].update(
        segment=Segment.FNO,
        instrument_type=InstrumentType.OPTION,
        expiry_date=expiry,
        strike_price=Decimal(1000),
        option_type=OptionType.CE,
        underlying="TEST",
    )
    payload["reference_inputs"].update(
        costs=None,
        contract_source=LongOptionContractSource(
            instrument_id="first",
            data_origin=DataOrigin.REPLAY,
            source="synthetic historical option policy",
            known_at=now,
            valid_from=now,
            valid_until=now + timedelta(minutes=5),
            risk_cost_reserve_per_unit=Decimal(".5"),
            kind="LONG_OPTION",
            minimum_days_to_expiry=2,
        ),
    )
    payload["fees"][0].update(segment=Segment.FNO, charge_basis="OPTION_PREMIUM")
    day = now.astimezone(IST).strftime("%d-%b-%Y").upper()
    payload["option_ban_report"] = {
        "origin": DataOrigin.REPLAY,
        "known_at": now,
        "document": f"Securities in Ban For Trade Date {day}:\n",
        "confirmation": "ADMIT PAPER NSE BAN REPORT",
        "reason": "Synthetic dated replay source",
    }
    return recording, HistoricalManifest.model_validate(payload)


@pytest.mark.parametrize("seconds,expected", [(5, "COMPLETED"), (4, "INCOMPLETE")])
async def test_option_recording_runs_actual_costed_oms(
    isolated_database,
    fake_clock,
    tmp_path,
    seconds,
    expected,
):
    recording, request = recorded_option()
    request = request.model_copy(update={"end_at": request.start_at + timedelta(seconds=seconds)})
    fake_clock.set_to(request.start_at)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "option.lock",
    )
    assert await run_prepared(worker, request) == expected, worker.detail
    async with db_session.session_scope() as session:
        run = await session.get(BacktestRun, request.run_id)
        result = await session.scalar(sa.select(BacktestResult))
        position = await session.scalar(sa.select(Position))
        assert run.strategy_id == "long-option-breakout"
        assert position is not None and position.segment == Segment.FNO
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == (
            2 if seconds == 5 else 1
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "PAPER_OPTION_OBSERVATION")
            )
            > 0
        )
        if seconds == 5:
            journal = await session.scalar(
                sa.select(JournalEntry).where(JournalEntry.kind == "TRADE")
            )
            assert position.net_quantity == 0
            assert result.trade_count == 1
            assert result.gross_pnl == journal.gross_pnl == Decimal(-16)
            assert result.total_charges == journal.charges == Decimal("11.93")
            assert result.net_pnl == journal.net_pnl == Decimal("-27.93")
        else:
            assert position.net_quantity == 4
            assert result.trade_count == 0


@pytest.mark.parametrize("defect", ["absent", "future", "wrong_expiry"])
async def test_option_history_required_before_bootstrap_writes(
    isolated_database,
    fake_clock,
    tmp_path,
    defect,
):
    recording, request = recorded_option()
    chain = recording.snapshots[1]
    snapshots = [recording.snapshots[0], recording.snapshots[2]]
    if defect == "future":
        snapshots.append(chain.model_copy(update={"available_at": request.end_at}))
    elif defect == "wrong_expiry":
        snapshots.append(
            chain.model_copy(
                update={
                    "value": replace(
                        chain.value,
                        expiry=chain.value.expiry + timedelta(days=7),
                    )
                }
            )
        )
    recording = RecordingBundle.model_validate(
        recording.model_copy(update={"snapshots": tuple(snapshots)}).model_dump()
    )
    request = request.model_copy(update={"recording_sha256": recording.content_hash()})
    fake_clock.set_to(request.start_at)
    with pytest.raises(ValueError, match="OPTION_CHAIN_HISTORY_UNAVAILABLE"):
        await prepare_run(
            request,
            recording,
            settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
            clock=fake_clock,
            lock_path=tmp_path / "refused.lock",
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestRun)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


@pytest.mark.parametrize("defect", ["ban", "greeks"])
async def test_historical_option_inputs_cannot_bypass_entry_guards(
    isolated_database,
    fake_clock,
    tmp_path,
    defect,
):
    recording, request = recorded_option()
    if defect == "ban":
        report = request.option_ban_report
        request = request.model_copy(
            update={
                "option_ban_report": report.model_copy(
                    update={
                        "document": report.document + "1,TEST\n",
                    }
                )
            }
        )
    else:
        chain = recording.snapshots[1]
        strike = chain.value.strikes[0]
        changed = chain.model_copy(
            update={
                "value": replace(
                    chain.value,
                    strikes=(
                        replace(
                            strike,
                            call=replace(strike.call, greeks=None),
                        ),
                    ),
                )
            }
        )
        recording = RecordingBundle.model_validate(
            recording.model_copy(
                update={
                    "snapshots": (recording.snapshots[0], changed, recording.snapshots[2]),
                }
            ).model_dump()
        )
        request = request.model_copy(update={"recording_sha256": recording.content_hash()})
    fake_clock.set_to(request.start_at)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "blocked.lock",
    )
    await run_prepared(worker, request)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        assert (await session.scalar(sa.select(BacktestResult))).trade_count == 0


async def test_option_subprocess_replay_is_reproducible(tmp_path):
    fingerprints_seen = []
    for name in ("first", "repeat"):
        root = tmp_path / name
        root.mkdir()
        recording, request = recorded_option()
        engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'ats_history_fixture01.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            (root / "manifest.json").write_text(request.model_dump_json(), encoding="utf-8")
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "database_url": str(engine.url),
                        "starting_capital": "100000",
                    }
                ),
                encoding="utf-8",
            )
            write_recording(root / "recording.json", recording)
            outcome = await launch_async(
                root / "manifest.json",
                root / "recording.json",
                root / "settings.json",
                timeout_seconds=60,
            )
            assert outcome.returncode == 0, (outcome.stdout, outcome.stderr)
            async with async_sessionmaker(engine)() as session:
                catalog = await load_catalog(session, request.run_id)
                assert catalog["results"][0]["net_pnl"] == Decimal("-27.93")
                fingerprints_seen.append(fingerprints(catalog))
        finally:
            await engine.dispose()
    assert fingerprints_seen[0] == fingerprints_seen[1]


@pytest.mark.parametrize("defect", ["report", "future", "tariff", "strike", "futures", "multi_day"])
def test_option_manifest_requires_explicit_supported_inputs(defect):
    _recording, request = recorded_option()
    payload = request.model_dump()
    if defect == "report":
        payload["option_ban_report"] = None
    elif defect == "future":
        payload["option_ban_report"]["known_at"] = request.start_at + timedelta(seconds=1)
    elif defect == "tariff":
        payload["fees"][0].update(segment=Segment.CASH, charge_basis="CASH_TURNOVER")
    elif defect == "strike":
        payload["instruments"][0]["strike_price"] = None
    elif defect == "futures":
        payload["instruments"][0]["instrument_type"] = InstrumentType.FUTURE
    else:
        payload["end_at"] = request.end_at + timedelta(days=1)
    with pytest.raises(ValueError):
        HistoricalManifest.model_validate(payload)


async def test_option_plan_cannot_receive_equity_walkforward_attribution(tmp_path):
    registry, experiment = await experiment_files(tmp_path)
    identifier = experiment.candidates[0][0].train_plan
    recording, manifest = recorded_option()
    manifest = manifest.model_copy(
        update={
            "run_id": identifier,
            "end_at": manifest.start_at + timedelta(seconds=60, microseconds=-1),
        }
    )
    directory = tmp_path / identifier
    (directory / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    write_recording(directory / "option-recording.json", recording)
    plans = json.loads(registry.read_text(encoding="utf-8"))
    selected = next(plan for plan in plans["plans"] if plan["id"] == identifier)
    selected["recording"] = f"{identifier}/option-recording.json"
    registry.write_text(json.dumps(plans), encoding="utf-8")
    with pytest.raises(ValueError, match="strategy family must remain constant"):
        freeze_experiment(registry, experiment)
