"""Hand-specified count and cooldown boundaries; no invented runtime history."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.data_origin import DataOrigin
from app.core.enums import OrderStatus
from app.risk.engine import evaluate
from app.risk.entry_history import count_orders
from app.risk.entry_models import ClosedOutcome, EntryEvidence, EntryPolicy
from app.risk.entry_policy import apply_entry_policy, entry_rules
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_risk import limits, market, portfolio, proposal


def evidence(**changes):
    return EntryEvidence(
        **(
            {
                "policy": EntryPolicy(maximum_daily_entries=2, loss_cooloff_minutes=30),
                "as_of": OBSERVED,
                "mode": "PAPER",
                "data_origin": "SYNTHETIC",
                "instrument_id": "TEST",
                "counted_order_ids": (),
                "closed_outcomes": (),
            }
            | changes
        )
    )


def test_count_unknown_history_and_core_veto():
    approved = evaluate(proposal(), portfolio(), market(), limits())
    assert approved.approved
    assert apply_entry_policy(approved, evidence(counted_order_ids=("one",))).approved
    rejected = apply_entry_policy(approved, evidence(counted_order_ids=("one", "two")))
    assert not rejected.approved and rejected.approved_quantity == 0
    assert rejected.rejection_code == "DAILY_TRADE_COUNT_LIMIT"
    unknown = apply_entry_policy(approved, evidence(unavailable_reason="UNAVAILABLE"))
    assert unknown.rejection_code == "ENTRY_HISTORY_UNAVAILABLE"
    core = evaluate(proposal(), portfolio(), market(ban_listed=True), limits())
    assert apply_entry_policy(core, evidence()).rejection_code == "BAN_LISTED"
    with pytest.raises(ValidationError, match="duplicate"):
        evidence(counted_order_ids=("same", "same"))


def test_loss_unknown_cost_and_exact_release_boundary():
    for age, net, expected in (
        (1799, "-1", "LOSS_COOLOFF_ACTIVE"),
        (1800, "-1", None),
        (1, "0", None),
        (1, "1", None),
        (1, None, "RECENT_OUTCOME_UNAVAILABLE"),
    ):
        state = evidence(
            closed_outcomes=(
                ClosedOutcome(
                    journal_id="isolated",
                    closed_at=OBSERVED - timedelta(seconds=age),
                    net_pnl=net,
                ),
            )
        )
        rejected = next(
            (check.rejection_code for check in entry_rules(state) if not check.passed), None
        )
        assert rejected == expected
    with pytest.raises(ValidationError, match="future"):
        evidence(
            closed_outcomes=(
                ClosedOutcome(
                    journal_id="future",
                    closed_at=OBSERVED + timedelta(seconds=1),
                    net_pnl=-1,
                ),
            )
        )


def test_order_count_provenance_missing_fills_and_future_state():
    order = SimpleNamespace(
        id="one",
        status=OrderStatus.EXECUTED,
        created_at=OBSERVED,
        updated_at=OBSERVED,
        request_payload={"data_origin": "SYNTHETIC"},
    )
    assert count_orders([order], set(), DataOrigin.SYNTHETIC, OBSERVED, None) == (
        ["one"],
        "EXECUTED_ORDER_FILLS_UNAVAILABLE",
    )
    assert count_orders([order], {"one"}, DataOrigin.LIVE, OBSERVED, None) == ([], None)
    order.status = OrderStatus.CANCELLED
    assert count_orders([order], set(), DataOrigin.SYNTHETIC, OBSERVED, None) == ([], None)
    assert count_orders([order], {"one"}, DataOrigin.SYNTHETIC, OBSERVED, None) == (["one"], None)
    order.updated_at = OBSERVED + timedelta(seconds=1)
    assert count_orders([order], set(), DataOrigin.SYNTHETIC, OBSERVED, None) == (
        ["one"],
        "ORDER_STATE_NEWER_THAN_DECISION",
    )
