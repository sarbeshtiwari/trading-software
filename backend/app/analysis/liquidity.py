"""EQ-002: actual average traded value and position-aware participation constraints."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field

from app.analysis.equity import EquitySnapshot, EvidenceModel


class LiquidityPolicy(EvidenceModel):
    lookback: int = Field(ge=1, strict=True)
    minimum_traded_value: Decimal = Field(ge=0)
    max_participation: Decimal = Field(gt=0, le=1)
    max_spread_fraction: Decimal = Field(ge=0, le=1)
    max_quote_age_seconds: int = Field(gt=0, strict=True)
    max_history_age_days: int = Field(gt=0, strict=True)


class LiquidityResult(EvidenceModel):
    average_traded_value: Decimal | None
    intended_notional: Decimal
    spread_fraction: Decimal | None
    reasons: tuple[str, ...]


def screen_liquidity(
    snapshot: EquitySnapshot,
    *,
    quantity: int,
    as_of: datetime,
    policy: LiquidityPolicy,
) -> LiquidityResult:
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("positive intended quantity required")
    snapshot.require_as_of(as_of, timedelta(seconds=policy.max_quote_age_seconds))
    notional = snapshot.price.value * quantity
    reasons = []
    bars = snapshot.daily[-policy.lookback :]
    average = None
    if len(bars) != policy.lookback or any(bar.traded_value is None for bar in bars):
        reasons.append("TRADED_VALUE_UNAVAILABLE")
    elif as_of - bars[-1].closed_at > timedelta(days=policy.max_history_age_days):
        reasons.append("STALE_TRADED_VALUE")
    else:
        average = sum(bar.traded_value for bar in bars) / len(bars)
        if average < policy.minimum_traded_value:
            reasons.append("TRADED_VALUE_FLOOR")
        if notional > average * policy.max_participation:
            reasons.append("POSITION_PARTICIPATION")
    spread = None
    if snapshot.bid is None or snapshot.ask is None:
        reasons.append("SPREAD_UNAVAILABLE")
    else:
        spread = (snapshot.ask - snapshot.bid) / ((snapshot.ask + snapshot.bid) / 2)
        if spread > policy.max_spread_fraction:
            reasons.append("SPREAD")
    return LiquidityResult(
        average_traded_value=average,
        intended_notional=notional,
        spread_fraction=spread,
        reasons=tuple(reasons),
    )
