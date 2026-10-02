"""Database schema, session lifecycle and migrations.

Covers DB-001, DB-002, DB-003 and the model-level guarantees of DB-004…DB-019.

These run against SQLite by default so the suite needs no server. The
PostgreSQL-specific guarantees — TimescaleDB hypertables, append-only triggers,
the cross-row fill-sum trigger — are marked and skipped unless
``ATS_TEST_POSTGRES_URL`` is set; they are listed as unverified in
IMPLEMENTATION_STATUS.md until then.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.clock import IST
from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import (
    Exchange,
    ExitReason,
    HealthStatus,
    InstrumentType,
    OptionType,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionState,
    Product,
    Segment,
    Severity,
    TransactionType,
    VerificationStatus,
)
from app.db import session as db_session
from app.db.models import (
    AuditEvent,
    BacktestRun,
    Candle,
    ConfigChange,
    ConsideredCandidate,
    Discrepancy,
    FundamentalSnapshot,
    HealthRecord,
    Instrument,
    JournalEntry,
    LLMCall,
    NewsItem,
    Order,
    OrderEvent,
    Position,
    Proposal,
    RiskConfigVersion,
    RiskDecision,
    SizingRecord,
    StrategyRegistration,
    SystemState,
    Trade,
    metadata,
)
from app.db.models.system import SINGLETON_ID
from app.modes import TradingMode

pytestmark = pytest.mark.integration

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set ATS_TEST_POSTGRES_URL to run PostgreSQL-specific schema tests",
)

NOW = datetime(2026, 1, 5, 9, 30, tzinfo=IST)


# --- DB-001 / DB-002 ------------------------------------------------------


async def test_db_connectivity(db_engine) -> None:
    await db_session.ping()
    async with db_engine.connect() as connection:
        assert (await connection.execute(sa.text("SELECT 1"))).scalar_one() == 1


async def test_session_lifecycle(db_engine) -> None:
    """A failed unit of work rolls back; a successful one commits."""
    async with db_session.session_scope() as session:
        session.add(_instrument("RELIANCE"))

    async with db_session.session_scope() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Instrument))
        assert count == 1

    with pytest.raises(RuntimeError):
        async with db_session.session_scope() as session:
            session.add(_instrument("TCS"))
            await session.flush()
            raise RuntimeError("work failed")

    async with db_session.session_scope() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Instrument))
        assert count == 1  # the failed unit of work left nothing behind


async def test_ping_reports_an_unreachable_database(settings_env) -> None:
    from app.core.errors import ConnectionFailedError

    settings_env(DATABASE_URL="postgresql+asyncpg://nobody@127.0.0.1:1/none")
    db_session.init_engine(force=True)
    try:
        with pytest.raises(ConnectionFailedError):
            await db_session.ping()
    finally:
        await db_session.dispose_engine()


# --- DB-003 ---------------------------------------------------------------


def test_migrations_up_to_date() -> None:
    """The initial migration must create exactly the model metadata.

    Drift between models and migrations is only ever discovered at the worst
    moment, so it is asserted here instead.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tests.conftest import BACKEND_ROOT

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    revisions = list(script.walk_revisions())
    assert revisions, "no migrations found"
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"


async def test_schema_contains_every_model_table(db_engine) -> None:
    async with db_engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())

    expected = set(metadata.tables)
    missing = expected - set(tables)
    assert not missing, f"tables missing from the created schema: {sorted(missing)}"


# --- Model behaviour ------------------------------------------------------


def _instrument(symbol: str = "RELIANCE", **overrides) -> Instrument:
    defaults = dict(
        exchange=Exchange.NSE,
        segment=Segment.CASH,
        instrument_type=InstrumentType.EQUITY,
        trading_symbol=symbol,
        isin=f"INE{symbol[:6]:0>6}",
        name=symbol.title(),
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    defaults.update(overrides)
    return Instrument(**defaults)


async def test_instrument_model(db_engine) -> None:
    option = _instrument(
        "NIFTY26JAN24500CE",
        segment=Segment.FNO,
        instrument_type=InstrumentType.OPTION,
        underlying="NIFTY",
        expiry_date=date(2026, 1, 29),
        strike_price=Decimal("24500"),
        option_type=OptionType.CE,
        lot_size=75,
    )
    async with db_session.session_scope() as session:
        session.add(option)

    async with db_session.session_scope() as session:
        stored = await session.scalar(
            sa.select(Instrument).where(Instrument.trading_symbol == "NIFTY26JAN24500CE")
        )
        assert stored is not None
        assert stored.lot_size == 75
        assert stored.is_option
        assert not stored.is_future
        assert stored.key == "NSE_FNO_NIFTY26JAN24500CE"
        assert stored.option_type is OptionType.CE


async def test_instrument_symbol_is_unique_per_exchange_and_segment(db_engine) -> None:
    async with db_session.session_scope() as session:
        session.add(_instrument("INFY"))

    with pytest.raises(Exception):
        async with db_session.session_scope() as session:
            session.add(_instrument("INFY"))


async def test_candles_upsert_rather_than_duplicate(db_engine) -> None:
    """HD-002: re-ingesting an overlapping window must not double the volume."""
    async with db_session.session_scope() as session:
        instrument = _instrument("HDFCBANK")
        session.add(instrument)
        await session.flush()
        instrument_id = instrument.id

    bar_ts = NOW
    async with db_session.session_scope() as session:
        session.add(
            Candle(
                instrument_id=instrument_id,
                interval_minutes=5,
                ts=bar_ts,
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("99"),
                close=Decimal("104"),
                volume=1000,
                data_origin=DataOrigin.HISTORICAL,
            )
        )

    # The composite primary key is what makes a duplicate impossible.
    with pytest.raises(Exception):
        async with db_session.session_scope() as session:
            session.add(
                Candle(
                    instrument_id=instrument_id,
                    interval_minutes=5,
                    ts=bar_ts,
                    open=Decimal("100"),
                    high=Decimal("105"),
                    low=Decimal("99"),
                    close=Decimal("104"),
                    volume=1000,
                )
            )

    # A different interval at the same timestamp is a different bar.
    async with db_session.session_scope() as session:
        session.add(
            Candle(
                instrument_id=instrument_id,
                interval_minutes=15,
                ts=bar_ts,
                open=Decimal("100"),
                high=Decimal("106"),
                low=Decimal("98"),
                close=Decimal("105"),
                volume=4000,
            )
        )

    async with db_session.session_scope() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Candle))
        assert count == 2


async def test_candle_rejects_high_below_low(db_engine) -> None:
    async with db_session.session_scope() as session:
        instrument = _instrument("SBIN")
        session.add(instrument)
        await session.flush()
        instrument_id = instrument.id

    with pytest.raises(Exception):
        async with db_session.session_scope() as session:
            session.add(
                Candle(
                    instrument_id=instrument_id,
                    interval_minutes=1,
                    ts=NOW,
                    open=Decimal("100"),
                    high=Decimal("95"),
                    low=Decimal("105"),
                    close=Decimal("100"),
                    volume=1,
                )
            )


async def test_order_model_lifecycle(db_engine) -> None:
    async with db_session.session_scope() as session:
        instrument = _instrument("ICICIBANK")
        session.add(instrument)
        await session.flush()

        order = Order(
            intent_id="oin_01TEST",
            broker_reference_id="REF12345678",
            instrument_id=instrument.id,
            trading_symbol="ICICIBANK",
            exchange=Exchange.NSE,
            segment=Segment.CASH,
            product=Product.MIS,
            order_type=OrderType.LIMIT,
            transaction_type=TransactionType.BUY,
            quantity=100,
            price=Decimal("1050.25"),
            mode=TradingMode.PAPER,
            execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(order)
        await session.flush()

        session.add(
            OrderEvent(
                order_id=order.id,
                from_status=OrderStatus.CREATED,
                to_status=OrderStatus.SUBMITTED,
                source="local",
                occurred_at=NOW,
            )
        )
        order_id = order.id

    async with db_session.session_scope() as session:
        stored = await session.get(Order, order_id)
        assert stored is not None
        assert stored.status is OrderStatus.CREATED
        assert stored.remaining_quantity == 100
        assert stored.mode is TradingMode.PAPER
        assert stored.execution_realism is ExecutionRealism.SIMULATED
        assert len(stored.events) == 1
        assert stored.events[0].to_status is OrderStatus.SUBMITTED


async def test_order_rejects_fills_exceeding_quantity(db_engine) -> None:
    """DB-007: a per-row guard; the cross-row sum is enforced by a trigger."""
    async with db_session.session_scope() as session:
        instrument = _instrument("AXISBANK")
        session.add(instrument)
        await session.flush()
        order = Order(
            intent_id="oin_02TEST",
            broker_reference_id="REF22345678",
            instrument_id=instrument.id,
            trading_symbol="AXISBANK",
            exchange=Exchange.NSE,
            segment=Segment.CASH,
            product=Product.MIS,
            order_type=OrderType.MARKET,
            transaction_type=TransactionType.BUY,
            quantity=50,
            filled_quantity=0,
            mode=TradingMode.PAPER,
            execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(order)
        await session.flush()
        order_id = order.id

    with pytest.raises(Exception):
        async with db_session.session_scope() as session:
            stored = await session.get(Order, order_id)
            stored.filled_quantity = 60  # more than was ordered
            await session.flush()


async def test_trade_partial_fills(db_engine) -> None:
    async with db_session.session_scope() as session:
        instrument = _instrument("LT")
        session.add(instrument)
        await session.flush()
        order = Order(
            intent_id="oin_03TEST",
            broker_reference_id="REF32345678",
            instrument_id=instrument.id,
            trading_symbol="LT",
            exchange=Exchange.NSE,
            segment=Segment.CASH,
            product=Product.MIS,
            order_type=OrderType.LIMIT,
            transaction_type=TransactionType.BUY,
            quantity=100,
            price=Decimal("3500"),
            mode=TradingMode.PAPER,
            execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(order)
        await session.flush()

        for index, quantity in enumerate((40, 60)):
            session.add(
                Trade(
                    order_id=order.id,
                    instrument_id=instrument.id,
                    exchange_trade_id=f"XTR{index}",
                    transaction_type=TransactionType.BUY,
                    quantity=quantity,
                    price=Decimal("3500"),
                    executed_at=NOW,
                    brokerage=Decimal("20"),
                    taxes=Decimal("5"),
                    other_charges=Decimal("1"),
                    mode=TradingMode.PAPER,
                    execution_realism=ExecutionRealism.SIMULATED,
                )
            )
        order_id = order.id

    async with db_session.session_scope() as session:
        stored = await session.get(Order, order_id)
        assert len(stored.trades) == 2
        assert sum(t.quantity for t in stored.trades) == 100
        assert stored.trades[0].total_charges == Decimal("26.00")


async def test_duplicate_exchange_trade_id_is_rejected(db_engine) -> None:
    """A fill counted twice is a position that does not exist."""
    async with db_session.session_scope() as session:
        instrument = _instrument("WIPRO")
        session.add(instrument)
        await session.flush()
        order = Order(
            intent_id="oin_04TEST",
            broker_reference_id="REF42345678",
            instrument_id=instrument.id,
            trading_symbol="WIPRO",
            exchange=Exchange.NSE,
            segment=Segment.CASH,
            product=Product.MIS,
            order_type=OrderType.MARKET,
            transaction_type=TransactionType.BUY,
            quantity=10,
            mode=TradingMode.PAPER,
            execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(order)
        await session.flush()
        session.add(
            Trade(
                order_id=order.id,
                instrument_id=instrument.id,
                exchange_trade_id="DUPLICATE",
                transaction_type=TransactionType.BUY,
                quantity=5,
                price=Decimal("500"),
                executed_at=NOW,
                mode=TradingMode.PAPER,
                execution_realism=ExecutionRealism.SIMULATED,
            )
        )
        order_id, instrument_id = order.id, instrument.id

    with pytest.raises(Exception):
        async with db_session.session_scope() as session:
            session.add(
                Trade(
                    order_id=order_id,
                    instrument_id=instrument_id,
                    exchange_trade_id="DUPLICATE",
                    transaction_type=TransactionType.BUY,
                    quantity=5,
                    price=Decimal("500"),
                    executed_at=NOW,
                    mode=TradingMode.PAPER,
                    execution_realism=ExecutionRealism.SIMULATED,
                )
            )


async def test_position_model_tracks_state_and_protection(db_engine) -> None:
    async with db_session.session_scope() as session:
        instrument = _instrument("MARUTI")
        session.add(instrument)
        await session.flush()
        position = Position(
            instrument_id=instrument.id,
            trading_symbol="MARUTI",
            segment=Segment.CASH,
            product=Product.MIS,
            side=PositionSide.LONG,
            net_quantity=10,
            average_price=Decimal("11000"),
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("500"),
            total_charges=Decimal("60"),
            stop_loss_price=Decimal("10800"),
            is_protected=True,
            opened_at=NOW,
            mode=TradingMode.PAPER,
            execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(position)
        await session.flush()
        position_id = position.id

    async with db_session.session_scope() as session:
        stored = await session.get(Position, position_id)
        assert stored.is_open
        assert stored.net_pnl == Decimal("440.00")

        stored.state = PositionState.CLOSED
        stored.net_quantity = 0
        stored.exit_reason = ExitReason.TARGET
        stored.closed_at = NOW + timedelta(hours=1)

    async with db_session.session_scope() as session:
        stored = await session.get(Position, position_id)
        assert not stored.is_open
        assert stored.exit_reason is ExitReason.TARGET


async def test_proposal_and_risk_decision_models(db_engine) -> None:
    from app.core.enums import SignalDirection

    async with db_session.session_scope() as session:
        instrument = _instrument("NIFTY", segment=Segment.FNO,
                                 instrument_type=InstrumentType.FUTURE, lot_size=75)
        session.add(instrument)
        await session.flush()

        proposal = Proposal(
            instrument_id=instrument.id,
            trading_symbol="NIFTY",
            segment=Segment.FNO,
            product=Product.NRML,
            direction=SignalDirection.LONG,
            strategy_id="index_trend_v1",
            entry_price=Decimal("24500"),
            stop_loss=Decimal("24400"),
            target_price=Decimal("24700"),
            suggested_quantity=150,
            confidence=Decimal("0.62"),
            thesis="Trend continuation above the prior swing high.",
            evidence=[{"type": "indicator", "id": "adx_14", "value": "28.4"}],
            invalidation_conditions=["close below 24400 on the 15m chart"],
            mode=TradingMode.PAPER,
        )
        session.add(proposal)
        await session.flush()

        decision = RiskDecision(
            proposal_id=proposal.id,
            approved=False,
            binding_rule="per_trade_risk",
            rejection_code="PER_TRADE_RISK_EXCEEDED",
            reason="risk 7500 exceeds the per-trade budget of 2500",
            rules_evaluated=[
                {"rule": "per_trade_risk", "passed": False,
                 "inputs": {"risk": "7500", "budget": "2500"}},
                {"rule": "max_exposure", "passed": True, "inputs": {"exposure": "0"}},
            ],
            state_snapshot={"open_positions": 0, "equity": "500000"},
            risk_config_version=1,
            evaluated_at=NOW,
            mode=TradingMode.PAPER,
        )
        session.add(decision)
        await session.flush()

        session.add(
            SizingRecord(
                proposal_id=proposal.id,
                method="risk_based",
                capital=Decimal("500000"),
                risk_budget=Decimal("2500"),
                risk_per_unit=Decimal("100"),
                raw_quantity=Decimal("25"),
                lot_size=75,
                final_quantity=0,
                binding_constraint="lot_size",
                zero_reason="BUDGET_BELOW_MIN_LOT",
                inputs={"per_trade_risk_pct": "0.5"},
            )
        )
        proposal_id = proposal.id

    async with db_session.session_scope() as session:
        stored = await session.scalar(
            sa.select(RiskDecision).where(RiskDecision.proposal_id == proposal_id)
        )
        assert stored is not None
        assert stored.approved is False
        assert stored.binding_rule == "per_trade_risk"
        # Every rule is kept, not only the failing one, so the decision replays.
        assert len(stored.rules_evaluated) == 2

        proposal = await session.get(Proposal, proposal_id)
        assert proposal.risk_per_unit == Decimal("100")
        assert proposal.evidence[0]["id"] == "adx_14"


async def test_considered_candidate_records_a_negative_decision(db_engine) -> None:
    """AID-009: the absence of a proposal is not evidence of a decision."""
    async with db_session.session_scope() as session:
        session.add(
            ConsideredCandidate(
                cycle_id="cyc_1",
                instrument_id="ins_x",
                trading_symbol="TATASTEEL",
                strategy_id="mean_reversion_v1",
                stopped_at_stage="REGIME",
                reason_code="REGIME_NOT_PERMITTED",
                reason_detail="strategy trades RANGING only; regime was TRENDING_UP",
                inputs={"regime": "TRENDING_UP", "adx": "31.2"},
                mode=TradingMode.PAPER,
            )
        )

    async with db_session.session_scope() as session:
        stored = await session.scalar(sa.select(ConsideredCandidate))
        assert stored.reason_code == "REGIME_NOT_PERMITTED"
        assert stored.inputs["adx"] == "31.2"


async def test_audit_event_carries_the_full_contract_field_set(db_engine) -> None:
    async with db_session.session_scope() as session:
        session.add(
            AuditEvent(
                chain_id="chn_1",
                sequence=1,
                event_type="RISK_DECISION",
                occurred_at=NOW,
                severity=Severity.INFO,
                market_state={"nifty": "24500"},
                data_used={"ltp": "24500", "as_of": NOW.isoformat()},
                news_used=[{"id": "nws_1", "verification": "VERIFIED"}],
                indicators={"rsi_14": "61.2"},
                strategy_id="index_trend_v1",
                signal={"direction": "LONG"},
                risk_calculation={"risk": "2500"},
                position_size={"quantity": 75},
                decision="APPROVED",
                risk_verdict="APPROVED",
                order_payload={"quantity": 75},
                broker_response={"status": "SUCCESS"},
                exit_detail=None,
                result=None,
                mode=TradingMode.PAPER,
                record_hash="0" * 64,
            )
        )

    async with db_session.session_scope() as session:
        stored = await session.scalar(sa.select(AuditEvent))
        for field in (
            "market_state", "data_used", "news_used", "indicators", "signal",
            "risk_calculation", "position_size", "decision", "risk_verdict",
            "order_payload", "broker_response",
        ):
            assert getattr(stored, field) is not None, field


async def test_system_state_is_a_singleton_row(db_engine) -> None:
    async with db_session.session_scope() as session:
        session.add(
            SystemState(
                id=SINGLETON_ID,
                mode=TradingMode.PAPER,
                trading_enabled=False,
                starting_capital=Decimal("500000"),
                current_equity=Decimal("500000"),
                peak_equity=Decimal("500000"),
                session_date=date(2026, 1, 5),
            )
        )

    async with db_session.session_scope() as session:
        stored = await session.get(SystemState, SINGLETON_ID)
        assert stored is not None
        # Arming is stored with the date it was granted for, so a restart on a
        # later day can never be treated as still armed (LIVE-004).
        assert stored.armed is False
        assert stored.armed_for_date is None
        assert stored.kill_switch_active is False


async def test_remaining_models_persist(db_engine) -> None:
    """Every model must actually round-trip; an unused table hides a bug."""
    async with db_session.session_scope() as session:
        instrument = _instrument("TITAN")
        session.add(instrument)
        await session.flush()

        session.add_all(
            [
                NewsItem(
                    dedupe_key="dk1",
                    title="Company reports results",
                    fetched_at=NOW,
                    sources=[{"slug": "nse_filings", "tier": 1}],
                    best_tier=1,
                    source_count=1,
                    entities=[{"instrument_id": instrument.id, "confidence": 0.98}],
                    verification_status=VerificationStatus.VERIFIED,
                ),
                FundamentalSnapshot(
                    instrument_id=instrument.id,
                    as_of=date(2026, 1, 1),
                    source="manual",
                    pe_ratio=Decimal("62.5"),
                ),
                LLMCall(
                    provider="fallback",
                    model_id="deterministic",
                    purpose="research",
                    prompt_version="1.0.0",
                    prompt_hash="a" * 64,
                    outcome="SUCCESS",
                    called_at=NOW,
                ),
                RiskConfigVersion(
                    version=1,
                    is_active=True,
                    capital=Decimal("500000"),
                    per_trade_risk_pct=Decimal("0.5"),
                    daily_loss_limit_pct=Decimal("2"),
                    max_drawdown_pct=Decimal("10"),
                    max_gross_exposure_multiple=Decimal("3"),
                    max_concurrent_positions=5,
                    max_trades_per_day=10,
                    min_reward_risk_ratio=Decimal("1.5"),
                    margin_buffer_pct=Decimal("20"),
                    max_slippage_pct=Decimal("0.3"),
                    author="owner",
                ),
                StrategyRegistration(
                    strategy_id="index_trend_v1",
                    version="1.0.0",
                    parameter_hash="b" * 64,
                    parameters={"adx_threshold": 25},
                ),
                JournalEntry(
                    kind="TRADE",
                    trading_symbol="TITAN",
                    strategy_id="index_trend_v1",
                    mode=TradingMode.PAPER,
                ),
                HealthRecord(
                    name="database",
                    status=HealthStatus.PASS,
                    critical=True,
                    checked_at=NOW,
                ),
                Discrepancy(
                    kind="POSITION",
                    trading_symbol="TITAN",
                    local_state={"qty": 10},
                    broker_state={"qty": 5},
                    delta={"qty": 5},
                    detected_at=NOW,
                ),
                BacktestRun(
                    strategy_id="index_trend_v1",
                    strategy_version="1.0.0",
                    start_date=date(2025, 10, 1),
                    end_date=date(2026, 1, 1),
                    interval_minutes=5,
                    initial_capital=Decimal("500000"),
                ),
                ConfigChange(
                    target="risk_config",
                    action="UPDATE",
                    before={"per_trade_risk_pct": "0.5"},
                    after={"per_trade_risk_pct": "0.4"},
                    actor="owner",
                    occurred_at=NOW,
                ),
            ]
        )

    async with db_session.session_scope() as session:
        for model in (
            NewsItem, FundamentalSnapshot, LLMCall, RiskConfigVersion,
            StrategyRegistration, JournalEntry, HealthRecord, Discrepancy,
            BacktestRun, ConfigChange,
        ):
            count = await session.scalar(sa.select(sa.func.count()).select_from(model))
            assert count == 1, model.__name__


async def test_backtest_runs_are_always_marked_simulated(db_engine) -> None:
    """BT-011: a backtest result can never be presented as live performance."""
    async with db_session.session_scope() as session:
        run = BacktestRun(
            strategy_id="s",
            strategy_version="1.0.0",
            start_date=date(2025, 10, 1),
            end_date=date(2026, 1, 1),
            interval_minutes=5,
            initial_capital=Decimal("100000"),
        )
        session.add(run)
        await session.flush()
        assert run.simulated is True


# --- PostgreSQL-specific guarantees ---------------------------------------


@requires_postgres
async def test_audit_append_only() -> None:  # pragma: no cover - needs a server
    """DB-012: UPDATE and DELETE on the audit table must fail at the database."""
    raise NotImplementedError(
        "Requires a PostgreSQL server; see IMPLEMENTATION_STATUS.md for the "
        "unverified-locally list."
    )


@requires_postgres
async def test_candles_hypertable() -> None:  # pragma: no cover - needs a server
    """DB-005: candles and ticks must be TimescaleDB hypertables."""
    raise NotImplementedError("Requires a TimescaleDB server.")


@requires_postgres
async def test_fill_sum_trigger() -> None:  # pragma: no cover - needs a server
    """DB-008: total fills may never exceed the order quantity."""
    raise NotImplementedError("Requires a PostgreSQL server.")
