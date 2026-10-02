"""Deterministic entry-only rules over recorded, point-in-time history."""

from app.risk.entry_models import EntryEvidence
from app.risk.models import RuleResult


def entry_rules(evidence):
    evidence = EntryEvidence.model_validate(evidence.model_dump())
    recent = [
        outcome
        for outcome in evidence.closed_outcomes
        if (evidence.as_of - outcome.closed_at).total_seconds()
        < evidence.policy.loss_cooloff_minutes * 60
    ]
    checks = (
        (
            "entry_history",
            evidence.unavailable_reason is None,
            "ENTRY_HISTORY_UNAVAILABLE",
            {"reason": evidence.unavailable_reason},
        ),
        (
            "daily_trade_count",
            len(evidence.counted_order_ids) < evidence.policy.maximum_daily_entries,
            "DAILY_TRADE_COUNT_LIMIT",
            {
                "count": len(evidence.counted_order_ids),
                "limit": evidence.policy.maximum_daily_entries,
            },
        ),
        (
            "recent_outcome",
            all(outcome.net_pnl is not None for outcome in recent),
            "RECENT_OUTCOME_UNAVAILABLE",
            {"journal_ids": ",".join(item.journal_id for item in recent)},
        ),
        (
            "loss_cooloff",
            not any(outcome.net_pnl is not None and outcome.net_pnl < 0 for outcome in recent),
            "LOSS_COOLOFF_ACTIVE",
            {"minutes": evidence.policy.loss_cooloff_minutes},
        ),
    )
    if evidence.window is not None:
        checks += (
            (
                "entry_window",
                evidence.window.phase == "INTRADAY",
                "ENTRY_WINDOW_BLOCKED",
                {
                    "phase": evidence.window.phase,
                    "calendar_source": evidence.window.calendar_source,
                },
            ),
        )
    return tuple(
        RuleResult(rule=name, passed=passed, rejection_code=code, inputs=inputs)
        for name, passed, code, inputs in checks
    )


def apply_entry_policy(decision, evidence):
    checks = entry_rules(evidence)
    rejection = next((check for check in checks if not check.passed), None)
    changes = {"rules": (*decision.rules, *checks), "entry_policy": evidence}
    if decision.approved and rejection:
        changes.update(
            approved=False,
            approved_quantity=0,
            binding_rule=rejection.rule,
            rejection_code=rejection.rejection_code,
        )
    return decision.model_copy(update=changes)
