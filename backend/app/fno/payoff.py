"""OPT-004: exact expiry payoff in currency units, before fees and slippage.

Underlying settlement is non-negative. Option quantities are lot multiples;
stock is counted in shares. Piecewise-linear extrema are evaluated at zero,
every strike and the final tail, not on an arbitrary price sampling grid.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import OptionType, TransactionType
from app.fno.options import OptionContract


class PayoffLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    contract: OptionContract
    side: TransactionType
    lots: int = Field(gt=0, strict=True)
    premium: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def price_on_tick(self):
        if self.premium % self.contract.tick_size:
            raise ValueError("premium is off tick")
        return self

    @property
    def quantity(self) -> int:
        return self.lots * self.contract.lot_size


class StockHolding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    underlying: str = Field(min_length=1)
    shares: int = Field(gt=0, strict=True)
    cost_per_share: Decimal = Field(gt=0)


class PayoffSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    max_loss: Decimal | None
    max_profit: Decimal | None
    breakevens: tuple[Decimal, ...]
    zero_intervals: tuple[tuple[Decimal, Decimal | None], ...]
    net_premium_paid: Decimal


def intrinsic_value(spot: Decimal, strike: Decimal, side: OptionType) -> Decimal:
    if not spot.is_finite() or spot < 0 or not strike.is_finite() or strike <= 0:
        raise ValueError("invalid settlement or strike")
    if side not in (OptionType.CE, OptionType.PE):
        raise ValueError("invalid option side")
    return max(Decimal(0), spot - strike if side == OptionType.CE else strike - spot)


def time_value(premium: Decimal, spot: Decimal, strike: Decimal, side: OptionType) -> Decimal:
    """Signed premium minus intrinsic; do not hide a below-intrinsic observation."""
    if not premium.is_finite() or premium < 0:
        raise ValueError("invalid premium")
    return premium - intrinsic_value(spot, strike, side)


def validate_legs(legs: tuple[PayoffLeg, ...], stock: StockHolding | None = None) -> None:
    if not legs:
        raise ValueError("at least one option leg required")
    identities = {
        (leg.contract.underlying, leg.contract.exchange, leg.contract.expiry) for leg in legs
    }
    if len(identities) != 1:
        raise ValueError("mixed underlying, exchange or expiry")
    if stock is not None and stock.underlying != legs[0].contract.underlying:
        raise ValueError("stock does not cover option underlying")


def payoff(
    legs: tuple[PayoffLeg, ...],
    settlement: Decimal,
    stock: StockHolding | None = None,
) -> Decimal:
    validate_legs(legs, stock)
    total = sum(
        (
            leg.side.sign
            * leg.quantity
            * (
                intrinsic_value(settlement, leg.contract.strike, leg.contract.option_type)
                - leg.premium
            )
            for leg in legs
        ),
        Decimal(0),
    )
    if stock is not None:
        total += stock.shares * (settlement - stock.cost_per_share)
    return total


def payoff_summary(
    legs: tuple[PayoffLeg, ...],
    stock: StockHolding | None = None,
) -> PayoffSummary:
    validate_legs(legs, stock)
    knots = sorted({Decimal(0), *(leg.contract.strike for leg in legs)})
    values = [payoff(legs, price, stock) for price in knots]
    tail = sum(
        leg.side.sign * leg.quantity for leg in legs if leg.contract.option_type == OptionType.CE
    ) + (stock.shares if stock else 0)
    roots = {price for price, value in zip(knots, values, strict=True) if value == 0}
    intervals = []
    for index in range(len(knots) - 1):
        left, right = knots[index : index + 2]
        low, high = values[index : index + 2]
        if low == high == 0:
            intervals.append((left, right))
        elif low * high < 0:
            roots.add(left - low * (right - left) / (high - low))
    if tail:
        root = knots[-1] - values[-1] / tail
        if root >= knots[-1]:
            roots.add(root)
    elif values[-1] == 0:
        intervals.append((knots[-1], None))
    return PayoffSummary(
        max_loss=None if tail < 0 else max(Decimal(0), -min(values)),
        max_profit=None if tail > 0 else max(Decimal(0), *values),
        breakevens=tuple(sorted(roots)),
        zero_intervals=tuple(intervals),
        net_premium_paid=sum(
            (leg.side.sign * leg.quantity * leg.premium for leg in legs), Decimal(0)
        ),
    )
