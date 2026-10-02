"""Recorded OOS threshold checks are necessary evidence, never trading permission."""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.analysis.equity import EvidenceModel


class OOSValidationPolicy(EvidenceModel):
    minimum_trades: int = Field(gt=0, strict=True)
    minimum_windows: int = Field(gt=0, strict=True)
    minimum_expectancy: Decimal = Field(ge=0)
    maximum_within_window_drawdown: Decimal = Field(ge=0, le=1)
    minimum_profitable_window_fraction: Decimal = Field(ge=0, le=1)


class EvidenceReview(EvidenceModel):
    simulated: Literal[True] = True
    status: Literal["PASSED", "FAILED", "UNAVAILABLE"]
    failures: tuple[str, ...]
    policy: OOSValidationPolicy | None
    live_approved: Literal[False] = False
    approval_blockers: tuple[str, ...] = (
        "STRATEGY_VERSION_EVIDENCE_BINDING_REQUIRED",
        "VERIFIED_BACKTEST_AND_EXTERNAL_DATA_REQUIRED",
        "TIME_BASED_PAPER_EVIDENCE_REQUIRED",
        "LIVE_ARMING_UNAVAILABLE",
    )
    audit_event_id: str | None = None


def evaluate_oos(policy, aggregate, *, integrity):
    if policy is None:
        return EvidenceReview(status="UNAVAILABLE", failures=("POLICY_UNAVAILABLE",), policy=None)
    if integrity != "AUDIT_BOUND" or aggregate is None:
        return EvidenceReview(
            status="UNAVAILABLE", failures=("AUDIT_BOUND_REPORT_REQUIRED",), policy=policy
        )
    diagnostics = (aggregate.window_parameters or {}).get("diagnostics", {})
    checks = (
        ("INSUFFICIENT_OOS_TRADES", aggregate.trade_count, policy.minimum_trades, "minimum"),
        (
            "INSUFFICIENT_OOS_WINDOWS",
            diagnostics.get("window_count"),
            policy.minimum_windows,
            "minimum",
        ),
        ("EXPECTANCY_BELOW_THRESHOLD", aggregate.expectancy, policy.minimum_expectancy, "minimum"),
        (
            "DRAWDOWN_EXCEEDS_THRESHOLD",
            diagnostics.get("max_within_window_drawdown"),
            policy.maximum_within_window_drawdown,
            "maximum",
        ),
        (
            "WINDOW_CONSISTENCY_BELOW_THRESHOLD",
            diagnostics.get("profitable_window_fraction"),
            policy.minimum_profitable_window_fraction,
            "minimum",
        ),
    )
    failures = []
    unavailable = False
    for reason, observed, threshold, comparison in checks:
        try:
            value = Decimal(str(observed))
            if not value.is_finite():
                raise ValueError("nonfinite metric")
        except (ArithmeticError, ValueError):
            unavailable = True
            failures.append(f"{reason}:METRIC_UNAVAILABLE")
            continue
        if (comparison == "minimum" and value < threshold) or (
            comparison == "maximum" and value > threshold
        ):
            failures.append(reason)
    return EvidenceReview(
        status="UNAVAILABLE" if unavailable else "FAILED" if failures else "PASSED",
        failures=tuple(failures),
        policy=policy,
    )
