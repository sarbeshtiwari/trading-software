"""Threshold boundaries cannot become LIVE permission, even for passing metrics."""

from types import SimpleNamespace

import pytest

from app.strategies.approval import OOSValidationPolicy, evaluate_oos
from app.strategies.evidence import StrategyBinding, summarize_bindings
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import specification_hash


def inputs():
    policy = OOSValidationPolicy(
        minimum_trades=100,
        minimum_windows=4,
        minimum_expectancy="10",
        maximum_within_window_drawdown="0.1",
        minimum_profitable_window_fraction="0.75",
    )
    report = SimpleNamespace(
        trade_count=100,
        expectancy="10",
        window_parameters={
            "diagnostics": {
                "window_count": 4,
                "max_within_window_drawdown": "0.1",
                "profitable_window_fraction": "0.75",
            }
        },
    )
    return policy, report


def test_exact_thresholds_pass_research_only_missing_evidence_still_blocks_live():
    policy, report = inputs()
    result = evaluate_oos(policy, report, integrity="AUDIT_BOUND")
    assert result.status == "PASSED" and not result.live_approved
    assert "TIME_BASED_PAPER_EVIDENCE_REQUIRED" in result.approval_blockers
    assert evaluate_oos(None, report, integrity="AUDIT_BOUND").status == "UNAVAILABLE"
    assert evaluate_oos(policy, report, integrity="LEGACY_UNBOUND").status == "UNAVAILABLE"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("trade_count", 99, "INSUFFICIENT_OOS_TRADES"),
        ("expectancy", "9.99", "EXPECTANCY_BELOW_THRESHOLD"),
        ("window_count", 3, "INSUFFICIENT_OOS_WINDOWS"),
        ("max_within_window_drawdown", "0.100001", "DRAWDOWN_EXCEEDS_THRESHOLD"),
        ("profitable_window_fraction", "0.749999", "WINDOW_CONSISTENCY_BELOW_THRESHOLD"),
    ],
)
def test_each_failed_threshold_refuses_evidence(field, value, reason):
    policy, report = inputs()
    if field in ("trade_count", "expectancy"):
        setattr(report, field, value)
    else:
        report.window_parameters["diagnostics"][field] = value
    result = evaluate_oos(policy, report, integrity="AUDIT_BOUND")
    assert result.status == "FAILED" and result.failures == (reason,)
    assert not result.live_approved


def test_unavailable_or_nonfinite_metrics_and_malformed_policies_fail_closed():
    policy, report = inputs()
    for missing in (None, "NaN", "Infinity", "invalid"):
        report.expectancy = missing
        result = evaluate_oos(policy, report, integrity="AUDIT_BOUND")
        assert result.status == "UNAVAILABLE" and not result.live_approved
    for invalid in (
        {"minimum_trades": 0},
        {"maximum_within_window_drawdown": "NaN"},
        {"minimum_profitable_window_fraction": "1.01"},
    ):
        with pytest.raises(ValueError):
            OOSValidationPolicy.model_validate(policy.model_dump() | invalid)


def test_strategy_bindings_refuse_changed_parameters_and_missing_legacy_evidence():
    first = ClosedCandleBreakout("first", "NSE:FIRST", "0.05", "0.005").spec
    second = ClosedCandleBreakout("first", "NSE:FIRST", "0.05", "0.0025").spec
    bound = StrategyBinding(specification=first, parameter_hash=specification_hash(first))
    changed = StrategyBinding(specification=second, parameter_hash=specification_hash(second))
    assert summarize_bindings([bound, bound]).state == "FIXED"
    assert summarize_bindings([bound, changed]).state == "MIXED"
    assert summarize_bindings([bound, None]).state == "UNAVAILABLE"
    assert summarize_bindings([]).state == "UNAVAILABLE"
    with pytest.raises(ValueError, match="parameter hash mismatch"):
        StrategyBinding(specification=second, parameter_hash=bound.parameter_hash)
