"""Stored decisions retain every rule and exact input for replay."""

import pytest

from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.risk.audit import RiskAudit
from tests.unit.test_risk import limits, market, portfolio, proposal, provenance


@pytest.mark.parametrize("quantity", [100, 251])
async def test_risk_decision_replay(db_engine, quantity):
    audit = RiskAudit()
    identifier, decision = await audit.evaluate_and_record(
        proposal(quantity=quantity), portfolio(), market(), limits()
    )
    assert decision.approved is (quantity == 100)
    assert await audit.replay(identifier) == decision
    async with db_session.session_scope() as session:
        row = await session.get(RiskDecision, identifier)
        assert len(row.rules_evaluated) == len(decision.rules)
        assert row.state_snapshot["config"]["per_trade_risk_pct"] == "0.5"
        row.approved = not row.approved
    with pytest.raises(ValueError, match="MISMATCH"):
        await audit.replay(identifier)


async def test_option_risk_versions_replay_without_weakening_current_evaluation(db_engine):
    audit = RiskAudit()
    proposed = proposal(is_option=True, defined_max_loss_per_unit=2)
    identifier, current = await audit.evaluate_and_record(
        proposed, portfolio(), market(greeks=provenance()), limits()
    )
    assert current.formula_version == "1.1.0"
    assert current.rejection_code == "OPTION_PREMIUM_UNDERSTATED"
    assert await audit.replay(identifier) == current
    async with db_session.session_scope() as session:
        row = await session.get(RiskDecision, identifier)
        snapshot = dict(row.state_snapshot)
        snapshot["decision"] = dict(snapshot["decision"], formula_version="1.0.0")
        row.state_snapshot = snapshot
    with pytest.raises(ValueError, match="MISMATCH"):
        await audit.replay(identifier)
    historical_rules = [
        check.model_dump(mode="json")
        for check in current.rules
        if check.rule not in ("option_short", "option_premium")
    ]
    assert all(check["passed"] for check in historical_rules)
    async with db_session.session_scope() as session:
        row = await session.get(RiskDecision, identifier)
        snapshot = dict(row.state_snapshot)
        historical = dict(snapshot["decision"])
        historical.pop("formula_version")
        historical.update(
            approved=True,
            approved_quantity=100,
            binding_rule=None,
            rejection_code=None,
            rules=historical_rules,
        )
        snapshot["decision"] = historical
        row.state_snapshot = snapshot
        row.rules_evaluated = historical_rules
        row.approved = True
        row.approved_quantity = 100
        row.binding_rule = None
        row.rejection_code = None
    replayed = await audit.replay(identifier)
    assert replayed.formula_version == "1.0.0" and replayed.approved
    assert replayed.risk_amount == 200
    _, repeated = await audit.evaluate_and_record(
        proposed, portfolio(), market(greeks=provenance()), limits()
    )
    assert repeated.rejection_code == "OPTION_PREMIUM_UNDERSTATED"
