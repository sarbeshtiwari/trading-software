"""Black-Scholes pricing — GRK-001.

Indian index and stock options are European-style, so Black-Scholes is the right
model rather than an approximation to a binomial tree.

**On floats.** Money and prices are Decimal everywhere else in this system
(ARCH-014). Option pricing is the deliberate exception: the model is
transcendental (it needs ``exp``, ``ln`` and the normal CDF), the standard
library provides those only in binary floating point, and a hand-rolled Decimal
``erf`` would be slower and less accurate than the C implementation. So the
arithmetic happens in float and the *result* is converted to Decimal at the
boundary. Double precision gives roughly 15 significant digits; option premia are
quoted to two decimal places, so the error is around ten orders of magnitude
below anything that matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.core.enums import OptionType
from app.core.errors import ValidationError

__all__ = [
    "BlackScholesInputs",
    "price",
    "d1_d2",
    "norm_cdf",
    "norm_pdf",
    "to_decimal",
    "intrinsic_value",
    "time_value",
]


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def to_decimal(value: float, places: str = "0.000001") -> Decimal:
    """Convert a computed float to Decimal at the module boundary."""
    return Decimal(str(value)).quantize(Decimal(places))


@dataclass(frozen=True)
class BlackScholesInputs:
    """Everything the model needs, validated once.

    ``time_to_expiry`` is in **years**. ``rate`` and ``dividend_yield`` are
    annualised decimals (0.065 for 6.5%), not percentages — mixing the two is the
    single most common way to get a delta that looks almost right.
    """

    spot: float
    strike: float
    time_to_expiry: float
    volatility: float
    rate: float = 0.065
    dividend_yield: float = 0.0
    option_type: OptionType = OptionType.CE

    def __post_init__(self) -> None:
        if self.spot <= 0:
            raise ValidationError(f"spot must be positive, got {self.spot}")
        if self.strike <= 0:
            raise ValidationError(f"strike must be positive, got {self.strike}")
        if self.time_to_expiry < 0:
            raise ValidationError(
                f"time_to_expiry must not be negative, got {self.time_to_expiry}"
            )
        if self.volatility < 0:
            raise ValidationError(f"volatility must not be negative, got {self.volatility}")

    @property
    def is_expired(self) -> bool:
        return self.time_to_expiry <= 0

    @property
    def carry(self) -> float:
        """Cost of carry: rate minus dividend yield."""
        return self.rate - self.dividend_yield


def d1_d2(inputs: BlackScholesInputs) -> tuple[float, float]:
    """The two standard Black-Scholes terms."""
    if inputs.is_expired or inputs.volatility == 0:
        raise ValidationError(
            "d1/d2 are undefined at zero time to expiry or zero volatility; "
            "use intrinsic value instead"
        )
    sigma_sqrt_t = inputs.volatility * math.sqrt(inputs.time_to_expiry)
    d1 = (
        math.log(inputs.spot / inputs.strike)
        + (inputs.carry + 0.5 * inputs.volatility**2) * inputs.time_to_expiry
    ) / sigma_sqrt_t
    return d1, d1 - sigma_sqrt_t


def intrinsic_value(spot: float, strike: float, option_type: OptionType) -> float:
    if option_type is OptionType.CE:
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def price(inputs: BlackScholesInputs) -> Decimal:
    """Theoretical option price.

    At (or past) expiry, and at zero volatility, the price *is* the intrinsic
    value — returning a model price there would invent time value that cannot
    exist.
    """
    if inputs.is_expired or inputs.volatility == 0:
        return to_decimal(intrinsic_value(inputs.spot, inputs.strike, inputs.option_type))

    d1, d2 = d1_d2(inputs)
    discount = math.exp(-inputs.rate * inputs.time_to_expiry)
    carry_discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)

    if inputs.option_type is OptionType.CE:
        value = inputs.spot * carry_discount * norm_cdf(d1) - inputs.strike * discount * norm_cdf(d2)
    else:
        value = inputs.strike * discount * norm_cdf(-d2) - inputs.spot * carry_discount * norm_cdf(
            -d1
        )
    return to_decimal(max(value, 0.0))


def time_value(inputs: BlackScholesInputs, market_price: Optional[float] = None) -> Decimal:
    """Premium above intrinsic value, from the model or from a market price."""
    intrinsic = intrinsic_value(inputs.spot, inputs.strike, inputs.option_type)
    reference = market_price if market_price is not None else float(price(inputs))
    return to_decimal(max(reference - intrinsic, 0.0))
