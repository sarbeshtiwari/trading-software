"""Deterministic synthetic account states; no live-order or alert-delivery claims."""

import logging
import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.clock import UTC
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.monitoring.gate import TradingGate, get_trading_gate
from app.risk.safety import RiskSafety
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_risk import limits, market, portfolio


@pytest.fixture(autouse=True)
def time_control(fake_clock):
    fake_clock.set_to(OBSERVED)


def service():
    gate = TradingGate()
    gate.clear("startup")
    return RiskSafety(gate=gate)


@pytest.mark.parametrize(
    "realised,unrealised,expected",
    [
        (-1999, 0, False),
        (-2000, 0, True),
        (-1100, -900, True),
        (-2001, 0, True),
    ],
)
async def test_daily_boundary(db_engine, realised, unrealised, expected):
    safety = service()
    state = await safety.observe(
        portfolio(realised_day_pnl=realised, unrealised_day_pnl=unrealised), market(), limits()
    )
    assert state.daily_loss is expected
    assert safety.gate.new_entries_allowed is not expected
    assert safety.gate.trading_enabled


async def test_rebound_restart_and_critical_audit(db_engine, caplog):
    safety = service()
    with caplog.at_level(logging.CRITICAL):
        await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    assert "latch engaged" in caplog.text
    await db_session.dispose_engine()
    db_session.init_engine()
    restarted = service()
    assert (await restarted.restore("PAPER", "SYNTHETIC")).daily_loss
    assert not restarted.gate.new_entries_allowed
    assert (await restarted.observe(portfolio(), market(), limits())).daily_loss
    chain = safety.chain_id("PAPER", "SYNTHETIC")
    records = await AuditService().chain(chain)
    assert [row.severity.value for row in records] == ["CRITICAL", "INFO"]
    assert await AuditService().verify(chain)


async def test_rollover_is_forward_fresh_ist_and_preserves_other_blocks(db_engine, fake_clock):
    safety = service()
    await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    tomorrow = OBSERVED.replace(hour=0, minute=0) + timedelta(days=1)
    fake_clock.set_to(tomorrow)
    safety.gate.block("reconciliation", "unresolved")
    state = await safety.observe(
        portfolio(observed_at=tomorrow.astimezone(UTC), available_at=tomorrow),
        market(as_of=tomorrow),
        limits(),
    )
    assert not state.daily_loss and state.session_date == tomorrow.date()
    assert not safety.gate.new_entries_allowed and safety.gate.trading_enabled
    assert "unresolved" in safety.gate.reason()
    records = await AuditService().chain(safety.chain_id("PAPER", "SYNTHETIC"))
    assert records[-1].result["before"]["daily_loss"] is True
    assert records[-1].result["state"]["daily_loss"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at": OBSERVED - timedelta(seconds=31)},
        {"available_at": OBSERVED + timedelta(seconds=1)},
        {"observed_at": OBSERVED + timedelta(seconds=1)},
        {"data_origin": "LIVE"},
    ],
)
async def test_bad_evidence_does_not_clear_latch(db_engine, changes):
    safety = service()
    await safety.observe(portfolio(realised_day_pnl=-2000), market(), limits())
    with pytest.raises(ValueError):
        await safety.observe(portfolio(**changes), market(), limits())
    assert (await service().restore("PAPER", "SYNTHETIC")).daily_loss
    assert not safety.gate.new_entries_allowed


async def test_backwards_and_future_decision_time(db_engine, fake_clock):
    safety = service()
    await safety.observe(portfolio(), market(), limits())
    previous = OBSERVED - timedelta(seconds=1)
    with pytest.raises(ValueError, match="backwards"):
        await safety.observe(
            portfolio(observed_at=previous, available_at=previous), market(), limits()
        )
    with pytest.raises(ValueError, match="future"):
        await safety.observe(portfolio(), market(as_of=OBSERVED + timedelta(seconds=1)), limits())


async def test_sticky_drawdown_and_error_across_days(db_engine, fake_clock):
    safety = service()
    assert (await safety.observe(portfolio(equity=90000), market(), limits())).drawdown
    await safety.trip_error("PAPER", "SYNTHETIC")
    later = OBSERVED + timedelta(days=1)
    fake_clock.set_to(later)
    restarted = service()
    state = await restarted.observe(
        portfolio(observed_at=later, available_at=later), market(as_of=later), limits()
    )
    assert state.drawdown and state.engine_error and state.code == "RISK_ERROR_LATCHED"
    assert restarted.gate.trading_enabled and not restarted.gate.new_entries_allowed
    assert not hasattr(restarted, "reset")


async def test_reserved_budget_does_not_trip_actual_loss_latch(db_engine):
    assert not (
        await service().observe(portfolio(reserved_risk=100000), market(), limits())
    ).daily_loss
    assert (
        await service().observe(
            portfolio(realised_day_pnl=-500), market(), limits(max_daily_loss_amount=500)
        )
    ).daily_loss


async def test_storage_failure_no_approval_and_recovery_stays_blocked(db_engine, monkeypatch):
    context = await setup_context()
    original = AuditService.append_in_session

    async def broken(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(AuditService, "append_in_session", broken)
    pipeline = DecisionPipeline(validator())
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await quant_decision(pipeline, payload(), context)
    assert not get_trading_gate().new_entries_allowed
    assert get_trading_gate().trading_enabled
    monkeypatch.setattr(AuditService, "append_in_session", original)
    assert (await quant_decision(pipeline, payload(), context)).approved_quantity == 0
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_pipeline_rebound_blocked_before_sizing(db_engine, monkeypatch):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    breached = context.model_copy(
        update={"portfolio": portfolio(strategy_id="test-only", realised_day_pnl=-2000)}
    )

    def forbidden(*args):
        raise AssertionError("blocked entry reached sizing")

    monkeypatch.setattr("app.agents.pipeline.size_position", forbidden)
    assert (await quant_decision(pipeline, payload(), breached)).code == "DAILY_LOSS_LATCHED"
    assert (
        await quant_decision(DecisionPipeline(validator()), payload(), context)
    ).code == "DAILY_LOSS_LATCHED"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_engine_exception_persists_disarm(db_engine, monkeypatch):
    context = await setup_context()

    def broken(*args):
        raise RuntimeError("injected rule failure")

    monkeypatch.setattr("app.risk.engine.RULES", (broken,))
    outcome = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert outcome.code == "RISK_ENGINE_ERROR" and outcome.approved_quantity == 0
    restarted = DecisionPipeline(validator())
    assert (await quant_decision(restarted, payload(), context)).code == "RISK_ERROR_LATCHED"
    assert (await service().restore("PAPER", "SYNTHETIC")).engine_error


async def test_unrelated_gate_blocks_and_tampered_ledger_fails_closed(db_engine):
    context = await setup_context()
    get_trading_gate().block("kill_switch", "owner stop", blocks_exits=True)
    assert (
        await quant_decision(DecisionPipeline(validator()), payload(), context)
    ).code == "TRADING_GATE_BLOCKED"
    assert not get_trading_gate().trading_enabled
    chain = RiskSafety.chain_id("PAPER", "SYNTHETIC")
    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(AuditEvent).where(AuditEvent.chain_id == chain))
        row.result = {"state": {}}
    with pytest.raises(ValueError, match="integrity"):
        await service().restore("PAPER", "SYNTHETIC")


async def test_optimistic_ledger_guard_rejects_stale_writer(db_engine):
    safety = service()
    await safety.observe(portfolio(), market(), limits())
    async with db_session.session_scope() as session:
        before, count = await safety._load(session, safety.chain_id("PAPER", "SYNTHETIC"))
    await safety.trip_error("PAPER", "SYNTHETIC")
    with pytest.raises(ValueError, match="concurrently"):
        async with db_session.session_scope() as session:
            await safety._record(
                session,
                "PAPER",
                "SYNTHETIC",
                before=before,
                after=before,
                evidence={},
                expected_count=count,
            )
    assert (await safety.restore("PAPER", "SYNTHETIC")).engine_error


async def test_mode_origin_ledgers_are_separate(db_engine):
    safety = service()
    await safety.trip_error("PAPER", "SYNTHETIC")
    assert not (await service().restore("PAPER", "LIVE")).engine_error
    assert not (await service().restore("SUPERVISED", "SYNTHETIC")).engine_error


async def test_separate_process_restores_persisted_latch(db_engine):
    await service().trip_error("PAPER", "SYNTHETIC")
    script = """
import asyncio
from app.db import session
from app.monitoring.gate import TradingGate
from app.risk.safety import RiskSafety
async def main():
    gate = TradingGate()
    gate.clear('startup')
    state = await RiskSafety(gate=gate).restore('PAPER', 'SYNTHETIC')
    assert state.engine_error and not gate.new_entries_allowed and gate.trading_enabled
    await session.dispose_engine()
asyncio.run(main())
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


async def test_late_latch_prevents_approval_commit(db_engine, monkeypatch):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    original = pipeline._evaluate

    async def latched_after_evaluation(proposal, context):
        decision = await original(proposal, context)
        await service().trip_error("PAPER", "SYNTHETIC")
        return decision

    monkeypatch.setattr(pipeline, "_evaluate", latched_after_evaluation)
    result = await quant_decision(pipeline, payload(), context)
    assert result.approved_quantity == 0
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_misconfigured_limits_latch_error(db_engine):
    context = await setup_context()
    broken = context.model_copy(
        update={"limits": context.limits.model_copy(update={"capital": Decimal(0)})}
    )
    result = await quant_decision(DecisionPipeline(validator()), payload(), broken)
    assert result.approved_quantity == 0
    assert (await service().restore("PAPER", "SYNTHETIC")).engine_error
