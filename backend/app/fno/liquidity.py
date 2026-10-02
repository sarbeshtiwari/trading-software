"""Option liquidity policy for selection; execution must recheck before ordering."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.marketdata.models import OptionLeg


class OptionLiquidityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    min_oi: int = Field(ge=0, strict=True)
    min_volume: int = Field(ge=0, strict=True)
    max_spread: Decimal = Field(ge=0)
    max_spread_fraction: Decimal = Field(ge=0)


def liquidity_failures(leg: OptionLeg, policy: OptionLiquidityPolicy) -> tuple[str, ...]:
    failures = []
    if leg.open_interest is None or leg.open_interest < policy.min_oi:
        failures.append("OPEN_INTEREST")
    if leg.volume is None or leg.volume < policy.min_volume:
        failures.append("VOLUME")
    if leg.bid is None or leg.ask is None:
        failures.append("SPREAD_UNAVAILABLE")
    elif not leg.bid.is_finite() or not leg.ask.is_finite() or leg.bid <= 0 or leg.ask < leg.bid:
        failures.append("INVALID_QUOTE")
    else:
        spread = leg.ask - leg.bid
        midpoint = (leg.ask + leg.bid) / 2
        if spread > policy.max_spread or spread / midpoint > policy.max_spread_fraction:
            failures.append("SPREAD")
    return tuple(failures)
