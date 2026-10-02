"""Pure deterministic evaluation; exceptions produce a rejection, never approval."""

from decimal import localcontext

from app.risk.config import RiskLimits
from app.risk.models import Decision, MarketState, PortfolioState, RiskProposal
from app.risk.rules import RULES


def evaluate(
    proposal: RiskProposal,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskLimits,
) -> Decision:
    try:
        proposal = RiskProposal.model_validate(proposal.model_dump())
        portfolio_state = PortfolioState.model_validate(portfolio_state.model_dump())
        market_state = MarketState.model_validate(market_state.model_dump())
        config = RiskLimits.model_validate(config.model_dump())
        with localcontext() as context:
            context.prec = 50
            checks = tuple(
                check
                for rule in RULES
                for check in rule(proposal, portfolio_state, market_state, config)
            )
            rejected = next((check for check in checks if not check.passed), None)
            return Decision(
                formula_version="1.1.0" if proposal.is_option else "1.0.0",
                approved=rejected is None,
                binding_rule=rejected.rule if rejected else None,
                rejection_code=rejected.rejection_code if rejected else None,
                approved_quantity=proposal.quantity if rejected is None else 0,
                risk_amount=proposal.planned_risk_per_unit * proposal.quantity,
                rules=checks,
            )
    except Exception:
        return Decision(
            approved=False,
            binding_rule="engine",
            rejection_code="RISK_ENGINE_ERROR",
            approved_quantity=0,
            risk_amount=0,
            rules=(),
        )
