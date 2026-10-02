"""Fail-closed bootstrap for one historical run in a dedicated empty database."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.backtest.adequacy import disclose_window, window_warning
from app.backtest.fno import require_chain_history
from app.backtest.universe import SURVIVORSHIP_NOTE, disclose_universe
from app.brokers.paper.engine import FillConfig
from app.config import HISTORICAL_EXECUTION_SETTINGS as EXECUTION_SETTINGS
from app.core.calendar import Holiday, SpecialSession, TradingCalendar
from app.core.clock import IST, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.db import session as db_session
from app.db.base import Base
from app.db.models.backtest import BacktestRun
from app.db.models.config import RiskConfigVersion
from app.db.models.instrument import Instrument
from app.execution.paper import PaperExecution
from app.fno.restrictions import BanAdmission, admit, parse_report
from app.modes import TradingMode
from app.portfolio.cost_store import CostStore
from app.portfolio.costs import FeeSchedule
from app.risk.config import RiskLimits
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry, specification_hash
from app.trading.contracts import LongOptionContractSource
from app.trading.inputs import ReferenceInputs, ReferenceInputStore
from app.trading.worker import PaperWorker, active_worker


class HistoricalInstrument(EvidenceModel):
    id: str = Field(min_length=1, max_length=40)
    trading_symbol: str = Field(min_length=1, max_length=64)
    exchange: Exchange
    segment: Literal[Segment.CASH, Segment.FNO]
    instrument_type: Literal[InstrumentType.EQUITY, InstrumentType.INDEX, InstrumentType.OPTION]
    expiry_date: date | None = None
    strike_price: Decimal | None = Field(default=None, gt=0)
    option_type: OptionType | None = None
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0)
    sector: str | None
    underlying: str | None
    is_active: bool
    is_restricted: bool
    source: str = Field(min_length=1, max_length=64)
    known_at: AwareDatetime

    @model_validator(mode="after")
    def contract_identity(self):
        if self.instrument_type == InstrumentType.OPTION:
            if (
                self.segment != Segment.FNO
                or self.exchange != Exchange.NSE
                or not self.underlying
                or self.expiry_date is None
                or self.strike_price is None
                or self.option_type is None
            ):
                raise ValueError("complete NSE option metadata required")
        elif self.segment != Segment.CASH or any(
            value is not None for value in (self.expiry_date, self.strike_price, self.option_type)
        ):
            raise ValueError("cash instrument cannot carry derivative identity")
        return self


class HistoricalManifest(EvidenceModel):
    version: Literal[1]
    run_id: str = Field(pattern=r"^[a-z0-9]{8,26}$")
    source: str = Field(min_length=1)
    recording_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    instruments: tuple[HistoricalInstrument, ...] = Field(min_length=1)
    strategy_instrument_id: str
    strategy_risk_fraction: Decimal = Field(gt=0, le=1)
    risk: RiskLimits
    fees: tuple[FeeSchedule, ...] = Field(min_length=1)
    reference_inputs: ReferenceInputs
    calendar_source: str = Field(min_length=1)
    calendar_known_at: AwareDatetime
    calendar_complete_years: tuple[int, ...]
    holidays: tuple[Holiday, ...]
    special_sessions: tuple[SpecialSession, ...]
    fill_config: FillConfig
    option_ban_report: BanAdmission | None = None

    @property
    def strategy_id(self):
        selected = next(item for item in self.instruments if item.id == self.strategy_instrument_id)
        return (
            "long-option-breakout"
            if selected.instrument_type == InstrumentType.OPTION
            else "closed-candle-breakout"
        )

    @model_validator(mode="after")
    def coherent(self):
        if self.start_at >= self.end_at or self.calendar_known_at > self.start_at:
            raise ValueError("invalid historical window or future calendar knowledge")
        if self.risk.version != 1 or self.fill_config.latency_ms < 0:
            raise ValueError("initial risk version or fill configuration invalid")
        if (
            not 0 <= self.fill_config.reject_probability <= 1
            or not 0 <= self.fill_config.partial_fill_probability <= 1
        ):
            raise ValueError("invalid fill probabilities")
        if not self.fill_config.slippage_bps.is_finite() or self.fill_config.slippage_bps < 0:
            raise ValueError("invalid fill slippage")
        identities = [item.id for item in self.instruments]
        keys = [(item.exchange, item.segment, item.trading_symbol) for item in self.instruments]
        if len(set(identities)) != len(identities) or len(set(keys)) != len(keys):
            raise ValueError("duplicate historical instrument identity")
        if self.strategy_instrument_id not in identities or any(
            item.known_at > self.start_at for item in self.instruments
        ):
            raise ValueError("missing strategy instrument or future instrument knowledge")
        if (
            self.reference_inputs.instrument_id != self.strategy_instrument_id
            or self.reference_inputs.data_origin != DataOrigin.REPLAY
        ):
            raise ValueError("historical reference inputs must match REPLAY instrument")
        self.reference_inputs.require_known(self.start_at)
        if self.reference_inputs.regime_source is None:
            raise ValueError("historical regime source required")
        self._check_regime_instruments()
        self._check_option_inputs()
        self.reference_inputs.regime_source.require_known(self.start_at)
        for fee in self.fees:
            fee.require_at(self.start_at)
        years = range(self.start_at.astimezone(IST).year, self.end_at.astimezone(IST).year + 1)
        if any(year not in self.calendar_complete_years for year in years):
            raise ValueError("historical calendar coverage incomplete")
        return self

    def calendar(self):
        return TradingCalendar(
            self.holidays,
            special_sessions=self.special_sessions,
            complete_years=self.calendar_complete_years,
            source=self.calendar_source,
        )

    def _check_regime_instruments(self):
        by_id = {item.id: item for item in self.instruments}
        regime_source = self.reference_inputs.regime_source
        index = by_id.get(regime_source.index_instrument_id)
        if index is None or index.instrument_type != InstrumentType.INDEX:
            raise ValueError("declared regime index metadata required")
        selected = by_id[self.strategy_instrument_id]
        if selected.instrument_type not in (InstrumentType.EQUITY, InstrumentType.OPTION):
            raise ValueError("reference strategy requires an equity or long-option instrument")
        if regime_source.provider_source is not None and any(
            identifier not in by_id or by_id[identifier].instrument_type != InstrumentType.EQUITY
            for identifier in regime_source.provider_source.breadth_instrument_ids
        ):
            raise ValueError("declared breadth constituent metadata required")

    def _check_option_inputs(self):
        selected = next(item for item in self.instruments if item.id == self.strategy_instrument_id)
        if selected.instrument_type != InstrumentType.OPTION:
            if self.option_ban_report is not None:
                raise ValueError("option ban report requires option strategy")
            return
        report = self.option_ban_report
        day = self.start_at.astimezone(IST).date()
        if (
            not isinstance(self.reference_inputs.contract_source, LongOptionContractSource)
            or report is None
            or report.origin != DataOrigin.REPLAY
            or report.known_at > self.start_at
            or report.expected_event_id is not None
            or parse_report(report.document).trade_date != day
            or self.end_at.astimezone(IST).date() != day
        ):
            raise ValueError("single-session option policy and point-in-time ban report required")
        if not any(
            fee.segment == Segment.FNO and fee.charge_basis == "OPTION_PREMIUM" for fee in self.fees
        ):
            raise ValueError("explicit historical option premium tariff required")


async def prepare_run(manifest, recording, *, settings, clock, lock_path):
    manifest = HistoricalManifest.model_validate(manifest.model_dump())
    require_chain_history(manifest, recording)
    window_disclosure = disclose_window(manifest, recording, settings.min_backtest_days)
    expected = f"ats_history_{manifest.run_id}"
    engine = db_session.get_engine()
    database = engine.url.database or ""
    actual = Path(database).stem if engine.dialect.name == "sqlite" else database
    if (
        actual != expected
        or get_clock() is not clock
        or active_worker() is not None
        or settings.trading_mode != TradingMode.PAPER
        or settings.broker_provider.value != "paper"
        or settings.starting_capital != manifest.risk.capital
        or clock.now() != manifest.start_at
        or recording.content_hash() != manifest.recording_sha256
    ):
        raise ValueError("historical database, process, mode, capital, clock or recording mismatch")
    async with db_session.session_scope() as session:
        for table in Base.metadata.sorted_tables:
            if await session.scalar(sa.select(sa.func.count()).select_from(table)):
                raise ValueError(
                    "historical database must be empty; existing account state refused"
                )
        session.add(
            BacktestRun(
                id=manifest.run_id,
                strategy_id=manifest.strategy_id,
                strategy_version="1",
                parameters=manifest.model_dump(mode="json"),
                seed=manifest.fill_config.seed,
                start_date=manifest.start_at.astimezone(IST).date(),
                end_date=manifest.end_at.astimezone(IST).date(),
                interval_minutes=1,
                universe=[item.trading_symbol for item in manifest.instruments],
                initial_capital=manifest.risk.capital,
                risk_config_snapshot=manifest.risk.model_dump(mode="json"),
                assumptions={
                    "window_disclosure": window_disclosure,
                    "execution_settings": settings.model_dump(
                        mode="json", include=set(EXECUTION_SETTINGS)
                    ),
                    "recording_sha256": manifest.recording_sha256,
                    "fill_convention": "RECORDED_DEPTH_AT_PUBLICATION"
                    if manifest.fill_config.use_depth
                    else "RECORDED_LTP_WITH_ADVERSE_SLIPPAGE",
                    "availability": "EXPLICIT_SNAPSHOTS_AND_NOMINAL_CANDLE_CLOSE",
                    "universe_disclosure": disclose_universe([manifest.model_dump(mode="json")]),
                },
                status="PREPARING",
                simulated=True,
                data_window_warning=window_warning(window_disclosure),
                survivorship_note=SURVIVORSHIP_NOTE,
            )
        )
        for item in manifest.instruments:
            session.add(Instrument(**item.model_dump(exclude={"known_at"})))
        values = manifest.risk.model_dump()
        fields = (
            "version",
            "capital",
            "per_trade_risk_pct",
            "daily_loss_limit_pct",
            "max_drawdown_pct",
            "max_gross_exposure_multiple",
            "max_concurrent_positions",
            "min_reward_risk_ratio",
            "margin_buffer_pct",
        )
        session.add(
            RiskConfigVersion(
                **{key: values[key] for key in fields},
                is_active=True,
                max_trades_per_day=settings.max_trades_per_day,
                max_slippage_pct=settings.max_slippage_pct,
                extra_limits={"engine_limits": manifest.risk.model_dump(mode="json")},
                author="historical-bootstrap",
                reason="Explicit isolated historical run configuration",
                activated_at=clock.utcnow(),
            )
        )
        await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id="historical-bootstrap",
                event_type="HISTORICAL_RUN_RESERVED",
                actor="historical-bootstrap",
                mode=TradingMode.PAPER,
            ),
            {"data_used": manifest.model_dump(mode="json"), "result": {"run_id": manifest.run_id}},
            expected_count=0,
        )
    try:
        if manifest.option_ban_report is not None:
            async with db_session.session_scope() as session:
                await admit(session, manifest.option_ban_report, actor="historical-bootstrap")
        for fee in manifest.fees:
            await CostStore(clock).publish(
                fee, actor="historical-bootstrap", reason="Explicit historical fee input"
            )
        await ReferenceInputStore(clock).publish(
            manifest.reference_inputs, actor="historical-bootstrap"
        )
        instrument = next(
            item for item in manifest.instruments if item.id == manifest.strategy_instrument_id
        )
        strategy = ClosedCandleBreakout(
            instrument.id,
            f"{instrument.exchange.value}:{instrument.trading_symbol}",
            instrument.tick_size,
            manifest.strategy_risk_fraction,
            clock=clock,
            long_option=instrument.instrument_type == InstrumentType.OPTION,
        )
        await StrategyRegistry().register(
            strategy, enabled_paper=True, actor="historical-bootstrap"
        )
        provider = recording.provider(clock=clock)
        executor = PaperExecution(
            provider.get_quote, settings=settings, clock=clock, fill_config=manifest.fill_config
        )
        await executor.recover()
        worker = PaperWorker(
            executor, provider=provider, calendar=manifest.calendar(), lock_path=lock_path
        )
        async with db_session.session_scope() as session:
            row = await session.get(BacktestRun, manifest.run_id)
            row.assumptions = row.assumptions | {
                "strategy_binding": {
                    "specification": strategy.spec.model_dump(mode="json"),
                    "parameter_hash": specification_hash(strategy.spec),
                }
            }
            row.status = "PREPARED"
        return worker
    except Exception as error:
        async with db_session.session_scope() as session:
            row = await session.get(BacktestRun, manifest.run_id)
            row.status = "FAILED"
            row.error_detail = type(error).__name__
            await AuditService(clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id="historical-bootstrap",
                    event_type="HISTORICAL_BOOTSTRAP_FAILED",
                    actor="historical-bootstrap",
                    mode=TradingMode.PAPER,
                ),
                {"result": {"run_id": manifest.run_id, "error_type": type(error).__name__}},
            )
        raise
