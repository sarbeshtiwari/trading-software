"""Health-check framework and the trading gate.

Covers MON-001, MON-004, and the honest-coverage reporting that keeps the
contract §10 checklist visible while the system is still being built.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.enums import HealthStatus
from app.core.errors import TradingDisabledError
from app.monitoring.checks import _expected_startup_checks
from app.monitoring.gate import TradingGate
from app.monitoring.healthchecks import (
    HealthCheck,
    HealthRegistry,
    get_health_registry,
)
from app.monitoring.startup import run_startup_checks, startup_check_coverage

pytestmark = pytest.mark.unit


class _Passing(HealthCheck):
    name = "passing"
    critical = True

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        return HealthStatus.PASS, "fine", {"x": 1}


class _FailingCritical(HealthCheck):
    name = "failing_critical"
    critical = True

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        return HealthStatus.FAIL, "broker unreachable", {}


class _FailingNonCritical(HealthCheck):
    name = "failing_optional"
    critical = False

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        return HealthStatus.FAIL, "news feed down", {}


class _Degraded(HealthCheck):
    name = "degraded"
    critical = False

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        return HealthStatus.DEGRADED, "using fallback", {}


class _Hanging(HealthCheck):
    name = "hanging"
    critical = True
    timeout_seconds = 0.05

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")


class _Exploding(HealthCheck):
    name = "exploding"
    critical = False

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        raise RuntimeError("check itself is broken")


# --- MON-001 --------------------------------------------------------------


async def test_healthcheck_framework() -> None:
    registry = HealthRegistry()
    registry.register(_Passing())
    registry.register(_Degraded())

    report = await registry.run_all()
    assert set(registry.names) == {"passing", "degraded"}
    assert len(report.results) == 2

    passing = next(r for r in report.results if r.name == "passing")
    assert passing.status is HealthStatus.PASS
    assert passing.critical
    assert passing.context == {"x": 1}
    assert passing.checked_at is not None

    # A degraded non-critical component does not stop trading, but is reported.
    assert report.trading_allowed
    assert report.overall_status is HealthStatus.DEGRADED
    assert registry.last_report is report


async def test_hanging_check_times_out_rather_than_stalling() -> None:
    registry = HealthRegistry()
    registry.register(_Hanging())

    report = await registry.run_all()
    result = report.results[0]
    assert result.status is HealthStatus.FAIL
    assert "timed out" in result.detail
    assert not report.trading_allowed


async def test_broken_check_is_a_failed_check_not_a_crash() -> None:
    registry = HealthRegistry()
    registry.register(_Exploding())

    report = await registry.run_all()
    assert report.results[0].status is HealthStatus.FAIL
    assert "RuntimeError" in report.results[0].detail
    assert "check itself is broken" not in report.results[0].detail
    # Non-critical, so trading is still allowed.
    assert report.trading_allowed


# --- MON-004 --------------------------------------------------------------


@pytest.mark.safety
async def test_critical_failure_disables_trading() -> None:
    registry = HealthRegistry()
    registry.register(_Passing())
    registry.register(_FailingCritical())

    report = await registry.run_all()
    assert not report.trading_allowed
    assert report.overall_status is HealthStatus.FAIL
    assert [r.name for r in report.failing_critical] == ["failing_critical"]
    assert "broker unreachable" in (report.blocking_reason() or "")


async def test_non_critical_failure_does_not_disable_trading() -> None:
    """A12: a news outage degrades the system; it does not stop trading."""
    registry = HealthRegistry()
    registry.register(_Passing())
    registry.register(_FailingNonCritical())

    report = await registry.run_all()
    assert report.trading_allowed
    assert report.overall_status is HealthStatus.DEGRADED
    assert report.blocking_reason() is None
    # The failure is still visible: it must never look like a healthy service.
    failing = next(r for r in report.results if r.name == "failing_optional")
    assert failing.status is HealthStatus.FAIL


# --- Trading gate ---------------------------------------------------------


def test_gate_starts_fail_closed() -> None:
    gate = TradingGate()
    assert not gate.new_entries_allowed
    assert not gate.trading_enabled
    assert "startup" in (gate.reason() or "")

    with pytest.raises(TradingDisabledError):
        gate.require_new_entries_allowed()
    with pytest.raises(TradingDisabledError):
        gate.require_trading_enabled()


def test_gate_opens_only_when_every_blocker_clears() -> None:
    gate = TradingGate()
    gate.clear("startup")
    assert gate.new_entries_allowed
    gate.require_new_entries_allowed()

    gate.block("daily_loss", "daily loss limit reached")
    gate.block("health", "market data stale")
    assert not gate.new_entries_allowed

    gate.clear("daily_loss")
    assert not gate.new_entries_allowed  # the other blocker still stands
    gate.clear("health")
    assert gate.new_entries_allowed


def test_entry_block_still_permits_exits() -> None:
    """A daily-loss breach must not strand an open position."""
    gate = TradingGate()
    gate.clear("startup")
    gate.block("daily_loss", "limit reached", blocks_exits=False)

    assert not gate.new_entries_allowed
    assert gate.trading_enabled  # exits are still allowed
    gate.require_trading_enabled()
    with pytest.raises(TradingDisabledError):
        gate.require_new_entries_allowed()


def test_state_snapshot_lists_every_reason() -> None:
    gate = TradingGate()
    gate.block("kill_switch", "activated by owner", blocks_exits=True)
    state = gate.state
    assert not state.trading_enabled
    assert not state.new_entries_allowed
    assert any("kill_switch" in reason for reason in state.reasons)
    assert state.to_dict()["trading_enabled"] is False


# --- Startup sequence -----------------------------------------------------


async def _seed_instrument() -> None:
    """A tradable universe of one: enough for the instruments check to pass."""
    from decimal import Decimal

    from app.core.enums import Exchange, InstrumentType, Segment
    from app.db import session as db_session
    from app.db.models.instrument import Instrument

    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQUITY,
                trading_symbol="RELIANCE",
                lot_size=1,
                tick_size=Decimal("0.05"),
            )
        )


async def test_startup_sets_the_gate_from_health(settings_env, db_engine) -> None:
    from app.modes import TradingMode, resolve_mode
    from app.monitoring.gate import get_trading_gate

    settings = settings_env(STARTING_CAPITAL="500000")
    resolve_mode(TradingMode.PAPER)
    await _seed_instrument()

    result = await run_startup_checks(settings)
    gate = get_trading_gate()

    assert result.report.results
    # The initial fail-closed blocker is replaced by the real health verdict.
    assert not any(reason.startswith("startup:") for reason in gate.state.reasons)
    assert result.trading_allowed, result.blocking_reason
    assert gate.new_entries_allowed


@pytest.mark.safety
async def test_empty_instrument_master_blocks_trading(settings_env, db_engine) -> None:
    """No lot sizes means no correct position sizing, so trading must not start."""
    from app.modes import TradingMode, resolve_mode

    settings = settings_env(STARTING_CAPITAL="500000")
    resolve_mode(TradingMode.PAPER)

    result = await run_startup_checks(settings)
    assert not result.trading_allowed
    assert "instruments" in (result.blocking_reason or "")


async def test_startup_leaves_trading_blocked_when_a_critical_check_fails(
    settings_env, db_engine
) -> None:
    """Capital is not configured, so the risk-config check fails and the gate holds."""
    from app.modes import TradingMode, resolve_mode
    from app.monitoring.gate import get_trading_gate

    settings = settings_env(STARTING_CAPITAL=None)
    resolve_mode(TradingMode.PAPER)

    result = await run_startup_checks(settings)

    assert not result.trading_allowed
    assert "risk_config" in (result.blocking_reason or "")
    assert not get_trading_gate().new_entries_allowed


async def test_startup_check_coverage_reports_missing_checks(
    settings_env, db_engine
) -> None:
    """MON-002 is only satisfied when all thirteen §10 checks exist.

    Until then the gap is reported rather than hidden, so nobody can mistake a
    missing check for a passing one.
    """
    settings_env(STARTING_CAPITAL="500000")
    from app.monitoring.checks import register_core_checks

    register_core_checks()
    present, missing = startup_check_coverage()

    expected = set(_expected_startup_checks())
    assert expected == set(present) | set(missing)
    assert "database" in present
    assert "redis" in present
    assert "system_clock" in present
    assert "risk_config" in present
    assert "groww_auth" in present
    assert "market_data" in present
    assert "instruments" in present
    assert "market_status" in present
    # Built in later phases; their absence must be explicit, never implied to pass.
    assert "news" in missing
    assert "order_service" in missing


async def test_missing_capital_fails_the_risk_config_check(settings_env, db_engine) -> None:
    settings = settings_env(STARTING_CAPITAL=None)
    from app.monitoring.checks import RiskConfigCheck

    result = await RiskConfigCheck(settings).execute()
    assert result.status is HealthStatus.FAIL
    assert result.critical
    assert "STARTING_CAPITAL" in result.detail


async def test_per_trade_risk_above_daily_limit_is_rejected(settings_env, db_engine) -> None:
    """A single trade must never be able to breach the daily limit on its own."""
    settings = settings_env(
        STARTING_CAPITAL="500000", PER_TRADE_RISK_PCT="3", DAILY_LOSS_LIMIT_PCT="2"
    )
    from app.monitoring.checks import RiskConfigCheck

    result = await RiskConfigCheck(settings).execute()
    assert result.status is HealthStatus.FAIL
    assert "DAILY_LOSS_LIMIT_PCT" in result.detail


async def test_memory_redis_is_degraded_in_paper_and_fatal_for_real_broker(
    settings_env,
) -> None:
    from app.monitoring.checks import RedisCheck

    paper = settings_env(TRADING_MODE="PAPER", REDIS_URL="memory://test")
    result = await RedisCheck(paper).execute()
    assert result.status is HealthStatus.DEGRADED
    assert not result.critical

    live = settings_env(
        TRADING_MODE="LIVE", BROKER_PROVIDER="groww", REDIS_URL="memory://test"
    )
    result = await RedisCheck(live).execute()
    assert result.status is HealthStatus.FAIL
    assert result.critical


async def test_database_check_detects_a_missing_schema(settings_env) -> None:
    """A reachable database with no tables is not a usable database."""
    settings_env()
    from app.db import session as db_session
    from app.monitoring.checks import DatabaseCheck

    db_session.init_engine(force=True)
    try:
        result = await DatabaseCheck().execute()
        assert result.status is HealthStatus.FAIL
        assert "schema is missing" in result.detail
    finally:
        await db_session.dispose_engine()


async def test_database_check_passes_with_schema(settings_env, db_engine) -> None:
    from app.monitoring.checks import DatabaseCheck

    result = await DatabaseCheck().execute()
    assert result.status is HealthStatus.PASS


async def test_mode_check_requires_credentials_for_real_broker(settings_env) -> None:
    from app.modes import TradingMode, resolve_mode
    from app.monitoring.checks import ModeCoherenceCheck

    settings = settings_env(TRADING_MODE="LIVE", BROKER_PROVIDER="groww")
    resolve_mode(TradingMode.LIVE)

    result = await ModeCoherenceCheck(settings).execute()
    assert result.status is HealthStatus.FAIL
    assert "credentials" in result.detail


def test_registry_is_process_wide_but_resettable() -> None:
    registry = get_health_registry()
    registry.register(_Passing())
    assert "passing" in registry.names
