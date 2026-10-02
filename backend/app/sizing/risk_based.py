"""SIZE-001…007/009: deterministic caps, not permission to execute an order."""

from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext

from app.sizing.lots import protective_ticks, round_lots
from app.sizing.models import SizingInputs, SizingPolicy, SizingResult


def size_position(inputs: SizingInputs, policy: SizingPolicy) -> SizingResult:
    inputs = SizingInputs.model_validate(inputs.model_dump())
    policy = SizingPolicy.model_validate(policy.model_dump())
    if policy.configured_capital is None:
        raise ValueError("STARTING_CAPITAL_UNAVAILABLE")
    if (
        not inputs.observed_at <= inputs.available_at <= inputs.as_of
        or inputs.as_of - inputs.observed_at > timedelta(seconds=policy.max_age_seconds)
    ):
        raise ValueError("STALE_OR_FUTURE_SIZING_EVIDENCE")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_FLOOR
        return _calculate(inputs, policy)


def _calculate(inputs: SizingInputs, policy: SizingPolicy) -> SizingResult:
    capital = min(policy.configured_capital, inputs.equity)
    entry, stop = protective_ticks(inputs.entry, inputs.stop, inputs.tick_size, inputs.direction)
    risk_per_unit = (
        max(abs(entry - stop), inputs.defined_max_loss_per_unit or Decimal(0))
        + inputs.risk_cost_per_unit
    )
    budgets = {"RISK": capital * min(policy.per_trade_risk_pct, inputs.requested_risk_pct) / 100}
    if inputs.atr is not None:
        budgets["VOLATILITY"] = budgets["RISK"] * min(Decimal(1), inputs.reference_atr / inputs.atr)
    if policy.kelly_enabled:
        if inputs.win_probability is None or inputs.payoff_ratio is None:
            raise ValueError("KELLY_EVIDENCE_UNAVAILABLE")
        fraction = max(
            Decimal(0), inputs.win_probability - (1 - inputs.win_probability) / inputs.payoff_ratio
        )
        budgets["KELLY"] = capital * min(fraction, policy.kelly_fraction_cap)
    budgets["DAILY_BUDGET"] = max(
        Decimal(0),
        capital * policy.daily_loss_limit_pct / 100 - inputs.daily_loss - inputs.reserved_risk,
    )
    budget_constraint = min(budgets, key=budgets.get)
    risk_budget = budgets[budget_constraint]
    caps = {budget_constraint: risk_budget / risk_per_unit}
    caps["MARGIN"] = (
        inputs.available_margin * (1 - policy.margin_buffer_pct / 100) / inputs.margin_per_unit
    )
    caps["GROSS_EXPOSURE"] = (
        max(Decimal(0), capital * policy.max_gross_exposure_multiple - inputs.gross_exposure)
        / inputs.exposure_per_unit
    )
    caps["CONCENTRATION"] = (
        max(
            Decimal(0),
            capital * policy.max_instrument_exposure_pct / 100 - inputs.instrument_exposure,
        )
        / inputs.exposure_per_unit
    )
    binding = min(caps, key=caps.get)
    raw_quantity = caps[binding]
    quantity = round_lots(raw_quantity, inputs.lot_size)
    return SizingResult(
        formula_version="1.1.0" if inputs.defined_max_loss_per_unit is not None else "1.0.0",
        quantity=quantity,
        capital=capital,
        entry=entry,
        stop=stop,
        risk_budget=risk_budget,
        risk_per_unit=risk_per_unit,
        raw_quantity=raw_quantity,
        allocated_risk=quantity * risk_per_unit,
        binding_constraint=binding,
        caps=caps,
        zero_reason="BUDGET_BELOW_MIN_LOT" if quantity == 0 else None,
    )
