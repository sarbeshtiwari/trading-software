"""Dedicated scratch-database bootstrap, never an existing PAPER account."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.pipeline import ContractCosts
from app.analysis.regime.events import EventCalendar
from app.analysis.regime.inputs import IndicatorPolicy
from app.backtest.bootstrap import HistoricalInstrument, HistoricalManifest, prepare_run
from app.brokers.paper.engine import FillConfig
from app.config import get_settings
from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, InstrumentType, Segment
from app.db import session as db_session
from app.db.base import Base
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestRun
from app.db.models.instrument import Instrument
from app.risk.active import active_limits
from app.strategies.registry import StrategyRegistry
from app.trading.inputs import ReferenceInputs
from app.trading.regime import RegimeSource
from tests.integration.test_proposal import validator
from tests.unit.test_costs import schedule
from tests.unit.test_recording_bundles import bundle
from tests.unit.test_regime import observation, policy
from tests.unit.test_replay_safety import BASE
from tests.unit.test_risk import limits


def manifest(recording):
    return HistoricalManifest(
        version=1,
        run_id="fixture01",
        source="isolated historical fixture",
        recording_sha256=recording.content_hash(),
        start_at=BASE,
        end_at=BASE + timedelta(hours=7),
        instruments=(
            HistoricalInstrument(
                id="first",
                trading_symbol="FIRST",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQUITY,
                lot_size=1,
                tick_size="0.05",
                sector="FIXTURE",
                underlying=None,
                is_active=True,
                is_restricted=False,
                source="fixture",
                known_at=BASE,
            ),
            HistoricalInstrument(
                id="index-required-before-execution",
                trading_symbol="INDEX-FIXTURE",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.INDEX,
                lot_size=1,
                tick_size="0.05",
                sector=None,
                underlying=None,
                is_active=True,
                is_restricted=False,
                source="fixture",
                known_at=BASE,
            ),
        ),
        strategy_instrument_id="first",
        strategy_risk_fraction="0.005",
        risk=limits(),
        fees=(schedule(known_at=BASE, effective_from=BASE, effective_to=BASE + timedelta(days=1)),),
        reference_inputs=ReferenceInputs(
            costs=ContractCosts(
                instrument_id="first",
                observed_at=BASE,
                available_at=BASE,
                source_id="fixture",
                data_origin=DataOrigin.REPLAY,
                margin_per_unit=20,
                exposure_per_unit=100,
                risk_cost_per_unit="0.5",
            ),
            validation=validator().policy,
            ban_listed=False,
            news_halt=False,
            manually_blocked=False,
            regime_source=RegimeSource(
                index_instrument_id="index-required-before-execution",
                indicators=IndicatorPolicy(
                    adx_period=14,
                    fast_period=5,
                    slow_period=20,
                    volatility_period=14,
                    periods_per_year=252 * 375,
                    bar_seconds=60,
                ),
                policy=policy(),
                implied_volatility=observation(20, at=BASE),
                breadth=observation("0.6", at=BASE),
                calendar=EventCalendar(
                    source="fixture",
                    known_at=BASE,
                    coverage_start=BASE,
                    coverage_end=BASE + timedelta(days=1),
                    events=(),
                ),
            ),
        ),
        calendar_source="isolated fixture calendar",
        calendar_known_at=BASE,
        calendar_complete_years=(2026,),
        holidays=(),
        special_sessions=(),
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=0),
    )


@pytest.fixture
async def isolated_database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ats_history_fixture01.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_bootstrap_prepares_real_services_and_refuses_reuse(
    isolated_database, fake_clock, tmp_path
):
    recording = bundle()
    request = manifest(recording)
    fake_clock.set_to(BASE)
    settings = get_settings().model_copy(update={"starting_capital": Decimal(100000)})
    worker = await prepare_run(
        request, recording, settings=settings, clock=fake_clock, lock_path=tmp_path / "run.lock"
    )
    assert not worker.running
    assert worker.executor.ready
    assert worker.executor.broker.account.starting_capital == Decimal(100000)
    assert worker.executor.settings.trading_mode.value == "PAPER"
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        assert row.status == "PREPARED" and row.simulated
        assert row.parameters["recording_sha256"] == recording.content_hash()
        assert await active_limits(session) == request.risk
        assert await session.get(Instrument, "first") is not None
        assert (
            await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "HISTORICAL_RUN_RESERVED")
            )
            is not None
        )
    with pytest.raises(ValueError, match="must be empty"):
        await prepare_run(
            request,
            recording,
            settings=settings,
            clock=fake_clock,
            lock_path=tmp_path / "other.lock",
        )


async def test_wrong_database_is_rejected_without_writes(db_engine, fake_clock, tmp_path):
    recording = bundle()
    fake_clock.set_to(BASE)
    with pytest.raises(ValueError, match="mismatch"):
        await prepare_run(
            manifest(recording),
            recording,
            settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
            clock=fake_clock,
            lock_path=tmp_path / "unused.lock",
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestRun)) == 0


async def test_failed_bootstrap_is_not_reported_prepared(
    isolated_database, fake_clock, tmp_path, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise ValueError("private fixture failure payload")

    monkeypatch.setattr(StrategyRegistry, "register", unavailable)
    recording = bundle()
    request = manifest(recording)
    fake_clock.set_to(BASE)
    settings = get_settings().model_copy(update={"starting_capital": Decimal(100000)})
    with pytest.raises(ValueError, match="private fixture"):
        await prepare_run(
            request,
            recording,
            settings=settings,
            clock=fake_clock,
            lock_path=tmp_path / "unused.lock",
        )
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        assert row.status == "FAILED" and row.error_detail == "ValueError"
        event = await session.scalar(
            sa.select(AuditEvent).where(AuditEvent.event_type == "HISTORICAL_BOOTSTRAP_FAILED")
        )
        assert event.result == {"run_id": request.run_id, "error_type": "ValueError"}


def test_manifest_refuses_future_knowledge_and_missing_capital():
    payload = manifest(bundle()).model_dump()
    payload["instruments"][0]["known_at"] = BASE + timedelta(seconds=1)
    with pytest.raises(ValueError, match="future instrument"):
        HistoricalManifest.model_validate(payload)
    payload = manifest(bundle()).model_dump()
    del payload["risk"]["capital"]
    with pytest.raises(ValueError):
        HistoricalManifest.model_validate(payload)


@pytest.mark.parametrize("problem", ["capital", "recording", "clock"])
async def test_mismatched_run_inputs_cannot_reserve_database(
    isolated_database,
    fake_clock,
    tmp_path,
    problem,
):
    recording = bundle()
    request = manifest(recording)
    fake_clock.set_to(BASE)
    settings = get_settings().model_copy(update={"starting_capital": Decimal(100000)})
    if problem == "capital":
        settings.starting_capital = None
    elif problem == "recording":
        request = request.model_copy(update={"recording_sha256": "0" * 64})
    with pytest.raises(ValueError, match="mismatch"):
        await prepare_run(
            request,
            recording,
            settings=settings,
            clock=FakeClock(BASE) if problem == "clock" else fake_clock,
            lock_path=tmp_path / "unused.lock",
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(BacktestRun)) == 0
