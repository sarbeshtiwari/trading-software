"""REG-001/003: deterministic classification with explicit confirmation and session cap.

Safety labels (event/high volatility/unknown) take effect immediately and bypass
ordinary transition limits. Recovery requires confirmation. Each input and policy
is retained in the decision; missing observations never become a normal regime.
"""

from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.regime.inputs import RegimeInputs
from app.core.clock import IST
from app.core.enums import MarketRegime


class RegimePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    adx_trending: Decimal = Field(gt=0, le=100)
    high_volatility: Decimal = Field(gt=0, le=10000)
    low_volatility: Decimal = Field(ge=0, le=10000)
    bullish_breadth: Decimal = Field(gt=Decimal("0.5"), le=1)
    bearish_breadth: Decimal = Field(ge=0, lt=Decimal("0.5"))
    confirmations: int = Field(ge=1, strict=True)
    max_changes_per_session: int = Field(ge=0, strict=True)
    max_age_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def volatility_order(self):
        if self.low_volatility >= self.high_volatility:
            raise ValueError("volatility thresholds must be ordered")
        return self


class RegimeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: MarketRegime
    candidate: MarketRegime
    reason: str
    inputs: RegimeInputs
    policy: RegimePolicy
    session: date
    changes: int = Field(ge=0)
    pending: MarketRegime | None
    pending_count: int = Field(ge=0)


FEATURES = (
    "adx",
    "ma_structure",
    "realised_volatility",
    "implied_volatility",
    "breadth",
    "event_risk",
)
SAFETY = {MarketRegime.EVENT_RISK, MarketRegime.HIGH_VOLATILITY, MarketRegime.UNKNOWN}


def evidence_fresh_at(decision: RegimeDecision, as_of) -> bool:
    if as_of < decision.inputs.as_of or (
        decision.inputs.valid_until is not None and as_of >= decision.inputs.valid_until
    ):
        return False
    observations = [getattr(decision.inputs, name) for name in FEATURES]
    return all(
        item is not None
        and item.observed_at <= item.available_at <= as_of
        and as_of - item.observed_at <= timedelta(seconds=decision.policy.max_age_seconds)
        for item in observations
    )


def candidate(inputs: RegimeInputs, policy: RegimePolicy) -> MarketRegime:
    fresh = {}
    for name in FEATURES:
        observation = getattr(inputs, name)
        if observation is not None and inputs.as_of - observation.observed_at <= timedelta(
            seconds=policy.max_age_seconds
        ):
            fresh[name] = observation.value
    if fresh.get("event_risk") == 1:
        return MarketRegime.EVENT_RISK
    if any(
        fresh.get(name, Decimal(-1)) >= policy.high_volatility
        for name in ("realised_volatility", "implied_volatility")
    ):
        return MarketRegime.HIGH_VOLATILITY
    if len(fresh) != len(FEATURES):
        return MarketRegime.UNKNOWN
    if max(fresh["realised_volatility"], fresh["implied_volatility"]) <= policy.low_volatility:
        return MarketRegime.LOW_VOLATILITY
    label = MarketRegime.RANGING
    if fresh["adx"] >= policy.adx_trending:
        if fresh["ma_structure"] == 1 and fresh["breadth"] >= policy.bullish_breadth:
            label = MarketRegime.TRENDING_UP
        if fresh["ma_structure"] == -1 and fresh["breadth"] <= policy.bearish_breadth:
            label = MarketRegime.TRENDING_DOWN
    return label


def classify(
    inputs: RegimeInputs,
    policy: RegimePolicy,
    previous: RegimeDecision | None = None,
) -> RegimeDecision:
    session = inputs.as_of.astimezone(IST).date()
    raw = candidate(inputs, policy)
    if previous is not None:
        if (previous.inputs.underlying, previous.inputs.data_origin) != (
            inputs.underlying,
            inputs.data_origin,
        ) or previous.policy != policy:
            raise ValueError("incompatible regime state or policy")
        if inputs.as_of <= previous.inputs.as_of:
            raise ValueError("regime observations must strictly advance")
    changes = previous.changes if previous is not None and previous.session == session else 0
    pending, count, reason, label = None, 0, "INITIAL", raw
    if previous is not None and previous.session == session:
        label, pending, count, changes, reason = _transition(raw, previous, policy)
    return RegimeDecision(
        label=label,
        candidate=raw,
        reason=reason,
        inputs=inputs,
        policy=policy,
        session=session,
        changes=changes,
        pending=pending,
        pending_count=count,
    )


def _transition(raw, previous, policy):
    if raw in SAFETY:
        return raw, None, 0, previous.changes, "SAFETY_OVERRIDE"
    if raw == previous.label:
        return raw, None, 0, previous.changes, "UNCHANGED"
    count = previous.pending_count + 1 if previous.pending == raw else 1
    if previous.changes >= policy.max_changes_per_session:
        return previous.label, raw, count, previous.changes, "SESSION_CHANGE_CAP"
    if count < policy.confirmations:
        return previous.label, raw, count, previous.changes, "AWAITING_CONFIRMATION"
    return raw, None, 0, previous.changes + 1, "CONFIRMED"
