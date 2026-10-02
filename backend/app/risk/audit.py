"""Persist complete risk decisions outside the pure engine and verify replay."""

from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.risk.config import RiskLimits
from app.risk.engine import evaluate
from app.risk.entry_policy import apply_entry_policy
from app.risk.models import Decision, MarketState, PortfolioState, RiskProposal


class RiskAudit:
    async def evaluate_and_record(
        self,
        proposal: RiskProposal,
        portfolio: PortfolioState,
        market: MarketState,
        config: RiskLimits,
        *,
        is_preflight: bool = False,
    ):
        decision = evaluate(proposal, portfolio, market, config)
        snapshot = {
            "proposal": proposal.model_dump(mode="json"),
            "portfolio": portfolio.model_dump(mode="json"),
            "market": market.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }
        async with db_session.session_scope() as session:
            row = RiskDecision(
                proposal_id=proposal.id,
                approved=decision.approved,
                binding_rule=decision.binding_rule,
                rejection_code=decision.rejection_code,
                rules_evaluated=[rule.model_dump(mode="json") for rule in decision.rules],
                state_snapshot=snapshot,
                risk_config_version=config.version,
                approved_quantity=decision.approved_quantity,
                risk_amount=decision.risk_amount,
                mode=market.mode,
                evaluated_at=market.as_of,
                is_preflight=is_preflight,
            )
            session.add(row)
            await session.flush()
            return row.id, decision

    async def replay(self, identifier: str) -> Decision:
        async with db_session.session_scope() as session:
            row = await session.get(RiskDecision, identifier)
            if row is None:
                raise ValueError("RISK_DECISION_UNAVAILABLE")
            snapshot = row.state_snapshot
            stored = Decision.model_validate(snapshot["decision"])
            decision = evaluate(
                RiskProposal.model_validate(snapshot["proposal"]),
                PortfolioState.model_validate(snapshot["portfolio"]),
                MarketState.model_validate(snapshot["market"]),
                RiskLimits.model_validate(snapshot["config"]),
            )
            if stored.formula_version == "1.0.0" and decision.formula_version == "1.1.0":
                historical_rules = tuple(
                    rule
                    for rule in decision.rules
                    if rule.rule not in ("option_short", "option_premium")
                )
                rejected = next((rule for rule in historical_rules if not rule.passed), None)
                decision = decision.model_copy(
                    update={
                        "formula_version": "1.0.0",
                        "rules": historical_rules,
                        "approved": rejected is None,
                        "binding_rule": rejected.rule if rejected else None,
                        "rejection_code": rejected.rejection_code if rejected else None,
                        "approved_quantity": 0 if rejected else snapshot["proposal"]["quantity"],
                    }
                )
            if stored.entry_policy is not None:
                decision = apply_entry_policy(decision, stored.entry_policy)
            if (
                decision != stored
                or row.approved != decision.approved
                or row.approved_quantity != decision.approved_quantity
                or row.binding_rule != decision.binding_rule
                or row.rejection_code != decision.rejection_code
                or row.risk_config_version != snapshot["config"]["version"]
                or row.rules_evaluated != [rule.model_dump(mode="json") for rule in decision.rules]
            ):
                raise ValueError("RISK_REPLAY_MISMATCH")
            return decision
