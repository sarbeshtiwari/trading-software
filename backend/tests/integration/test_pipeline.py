"""Isolated synthetic decisions through the shared path; no orders or live claims."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import ContractCosts, DecisionContext, DecisionPipeline
from app.agents.proposal import EvidenceReference, TradeProposal
from app.analysis.events import EventBlackout
from app.analysis.regime.classifier import classify
from app.core import calendar as calendar_module
from app.core.calendar import TradingCalendar
from app.core.clock import UTC
from app.core.enums import InstrumentType, Segment
from app.db import session as db_session
from app.db.models.decision import ConsideredCandidate, Proposal, RiskDecision, SizingRecord
from app.db.models.instrument import Instrument
from app.db.models.regime import RegimeHistory
from app.monitoring.gate import get_trading_gate
from app.risk.audit import RiskAudit
from app.sizing.audit import SizingAudit
from app.sizing.risk_based import size_position
from app.strategies.registry import StrategyRegistry
from app.strategies.signal import Signal
from tests.integration.test_proposal import evidence, validator
from tests.integration.test_proposal import market as validation_market
from tests.llm_fixture import receipt
from tests.quant_fixture import quant_decision
from tests.unit.test_costs import schedule
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_regime import inputs, policy
from tests.unit.test_risk import limits, market, portfolio, provenance
from tests.unit.test_strategies import FixtureStrategy
from tests.unit.test_strategies import signal_data as original_signal_data


def signal_data():
    return original_signal_data() | {"stop": 98, "targets": (104,)}


@pytest.fixture(autouse=True)
def time_control(fake_clock):
    fake_clock.set_to(OBSERVED)
    yield


async def setup_context():
    calendar_module._calendar = TradingCalendar(
        complete_years=[2026], source="isolated execution fixture calendar"
    )

    get_trading_gate().clear("startup")
    await evidence()
    strategy = FixtureStrategy()
    await StrategyRegistry().register(strategy, enabled_paper=True)
    decision = classify(inputs(), policy())
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "ins-test")
        instrument.sector = "TEST_SECTOR"
        session.add(
            RegimeHistory(
                id="reg-test",
                underlying="TEST",
                data_origin="SYNTHETIC",
                ts=OBSERVED.astimezone(UTC),
                decision=decision.model_dump(mode="json"),
            )
        )
    return DecisionContext(
        cycle_id="cycle-test",
        regime_id="reg-test",
        strategy=strategy.spec,
        prices=validation_market(),
        market=market(instrument_id="ins-test"),
        portfolio=portfolio(strategy_id="test-only"),
        limits=limits(),
        llm_available=True,
        costs=ContractCosts(
            **provenance(),
            instrument_id="ins-test",
            margin_per_unit=100,
            exposure_per_unit=100,
            risk_cost_per_unit=0,
        ),
    )


async def test_shared_contract_gate_refuses_option_tariff_for_future(db_engine):
    context = await setup_context()
    context = context.model_copy(
        update={
            "costs": context.costs.model_copy(
                update={
                    "fee_schedule": schedule(segment=Segment.FNO, charge_basis="OPTION_PREMIUM"),
                }
            )
        }
    )
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "ins-test")
        instrument.segment = Segment.FNO
        instrument.instrument_type = InstrumentType.FUTURE
        assert (
            DecisionPipeline(validator())._contract_gate(context, instrument)
            == "CONTRACT_FEE_SOURCE_MISMATCH"
        )


async def test_long_option_sizing_and_risk_floor_the_actual_tick_rounded_premium(db_engine):
    context = await setup_context()
    context = context.model_copy(
        update={
            "costs": context.costs.model_copy(
                update={
                    "defined_max_loss_per_unit": Decimal(99),
                    "margin_per_unit": Decimal(99),
                    "exposure_per_unit": Decimal(99),
                }
            )
        }
    )
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "ins-test")
        instrument.instrument_type = InstrumentType.OPTION
        instrument.segment = Segment.FNO
    proposed = TradeProposal.model_validate(payload(entry="100.01", target=301))
    pipeline = DecisionPipeline(validator())
    sizing, policy = pipeline._sizing(proposed, context, instrument, "option-fixture")
    result = size_position(sizing, policy)
    assert result.entry == Decimal("100.05")
    assert result.risk_per_unit == Decimal("100.05")
    assert result.quantity == 4
    risk = pipeline._risk_proposal(proposed, context, instrument, "option-fixture", result)
    assert risk.planned_risk_per_unit == Decimal("100.05")
    assert risk.margin_per_unit == risk.exposure_per_unit == Decimal("100.05")


async def test_shared_sizing_and_risk_use_same_defined_loss_basis(db_engine):
    context = await setup_context()
    context = context.model_copy(
        update={
            "costs": context.costs.model_copy(update={"defined_max_loss_per_unit": Decimal("100")})
        }
    )
    result = await quant_decision(DecisionPipeline(validator()), payload(target="300"), context)
    assert result.code == "RISK_APPROVED" and result.approved_quantity == 5
    async with db_session.session_scope() as session:
        sizing = await session.scalar(
            sa.select(SizingRecord).where(SizingRecord.proposal_id == result.proposal_id)
        )
        risk = await session.scalar(
            sa.select(RiskDecision).where(RiskDecision.proposal_id == result.proposal_id)
        )
        assert sizing.risk_per_unit == 100 and risk.risk_amount == 500
        sizing_id = sizing.id
    replayed = await SizingAudit().replay(sizing_id)
    assert replayed.quantity == 5 and replayed.formula_version == "1.1.0"


async def test_quant_path_parity(db_engine, fake_clock, monkeypatch):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    reference = await receipt(context, fake_clock, monkeypatch)
    llm = await pipeline.process(reference, context)
    quant = await pipeline.process(
        Signal(**signal_data()),
        context,
        evidence=(EvidenceReference(kind="FUNDAMENTAL", source_id="fund-test"),),
    )
    assert llm.code == quant.code == "RISK_APPROVED"
    assert llm.approved_quantity == quant.approved_quantity == 250
    async with db_session.session_scope() as session:
        rows = (await session.scalars(sa.select(Proposal))).all()
        assert {row.origin for row in rows} == {"QUANT", "LLM"}
        assert all(row.context_snapshot["regime_id"] == "reg-test" for row in rows)
        assert {row.suggested_quantity for row in rows} == {9999, None}
        sizing = (await session.scalars(sa.select(SizingRecord))).all()
        risks = (await session.scalars(sa.select(RiskDecision))).all()
    for row in sizing:
        assert (await SizingAudit().replay(row.id)).quantity == 250
    for row in risks:
        assert (await RiskAudit().replay(row.id)).approved_quantity == 250


async def test_confidence_floor_and_negative_decision(db_engine, monkeypatch):
    context = await setup_context()

    def forbidden(*args):
        raise AssertionError("risk must not run for rejected validation")

    monkeypatch.setattr("app.agents.pipeline.evaluate", forbidden)
    outcome = await DecisionPipeline(validator()).process(payload(confidence="0.5"), context)
    assert outcome.code == "CONFIDENCE_BELOW_FLOOR"
    async with db_session.session_scope() as session:
        candidate = await session.get(ConsideredCandidate, outcome.candidate_id)
        assert candidate.reason_code == outcome.code
        assert candidate.cycle_id == "cycle-test" and candidate.stopped_at_stage == "VALIDATION"
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_zero_size_and_risk_rejection_recorded(db_engine):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    zero = await quant_decision(
        pipeline,
        payload(),
        context.model_copy(
            update={"portfolio": portfolio(strategy_id="test-only", available_margin=0)}
        ),
    )
    assert zero.code == "BUDGET_BELOW_MIN_LOT" and zero.approved_quantity == 0
    rejected = await quant_decision(
        pipeline,
        payload(),
        context.model_copy(update={"market": market(instrument_id="ins-test", ban_listed=True)}),
    )
    assert rejected.code == "BAN_LISTED" and rejected.approved_quantity == 0
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SizingRecord)) == 2
        assert await session.scalar(sa.select(sa.func.count()).select_from(RiskDecision)) == 1


async def test_pipeline_context_and_mode_gates(db_engine):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    live = context.model_copy(update={"market": market(instrument_id="ins-test", mode="LIVE")})
    assert (await pipeline.process(payload(), live)).code == "STRATEGY_MODE_DISABLED"
    mismatch = context.model_copy(update={"prices": validation_market(instrument_id="other")})
    assert (
        await pipeline.process(payload(), mismatch)
    ).code == "CONTEXT_IDENTITY_OR_ORIGIN_MISMATCH"
    no_llm = context.model_copy(update={"llm_available": False})
    assert (await pipeline.process(payload(), no_llm)).code == "LLM_UNAVAILABLE"
    assert (
        await pipeline.process(
            Signal(**signal_data()),
            no_llm,
            evidence=(EvidenceReference(kind="FUNDAMENTAL", source_id="fund-test"),),
        )
    ).approved_quantity == 250
    assert (await pipeline.process(None, no_llm)).code == "NO_SIGNAL"


async def test_failed_persistence_never_returns_approval(db_engine, monkeypatch):
    context = await setup_context()
    original = AsyncSession.add

    def fail(self, instance, **kwargs):
        if isinstance(instance, RiskDecision):
            raise RuntimeError("injected persistence failure")
        return original(self, instance, **kwargs)

    monkeypatch.setattr(AsyncSession, "add", fail)
    with pytest.raises(RuntimeError, match="persistence failure"):
        await quant_decision(DecisionPipeline(validator()), payload(), context)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(SizingRecord)) == 0


async def test_stale_evidence_and_blackout_rejected(db_engine):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    stale = context.model_copy(
        update={
            "costs": context.costs.model_copy(
                update={"observed_at": OBSERVED - timedelta(seconds=61)}
            )
        }
    )
    assert (await pipeline.process(payload(), stale)).code == "STALE_OR_FUTURE_CONTEXT"
    blackout = EventBlackout(
        symbol="TEST",
        as_of=OBSERVED,
        status="BLACKOUT",
        event_ids=("event",),
        restricted_strategies=("test-only",),
    )
    assert (
        await pipeline.process(payload(), context.model_copy(update={"blackout": blackout}))
    ).code == "EVENT_BLACKOUT_OR_UNAVAILABLE"
    assert (
        await pipeline.process(payload(), context.model_copy(update={"regime_id": "missing"}))
    ).code == "REGIME_UNAVAILABLE"


async def test_tampering_and_signal_identity_are_audited(db_engine):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    assert (
        await pipeline.process(payload(override_risk=True), context)
    ).code == "CONTROL_TAMPER_ATTEMPT"
    bad_signal = Signal(**(signal_data() | {"strategy_version": "unregistered"}))
    assert (await pipeline.process(bad_signal, context)).code == "SIGNAL_CONTEXT_MISMATCH"
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(ConsideredCandidate)) == 2
        )
        assert await session.scalar(sa.select(sa.func.count()).select_from(RiskDecision)) == 0
