"""Analytic Greeks — GRK-002.

Delta, gamma, theta, vega and rho in closed form.

Conventions, stated because every vendor picks different ones and silent
mismatches are how a portfolio ends up hedged the wrong way:

* **Theta is per calendar day**, not per year. A theta of -12.8 means the option
  loses 12.8 points of value per day, which is the number a trader acts on.
* **Vega is per 1 volatility point** (a move from 12% to 13%), not per 1.0 of
  sigma.
* **Rho is per 1 percentage point** of the risk-free rate.
* Delta and gamma are per 1 unit of underlying, unscaled by lot size. Lot scaling
  happens in portfolio aggregation, where the lot size is actually known.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import GreekSource, OptionType
from app.fno.greeks.black_scholes import (
    BlackScholesInputs,
    d1_d2,
    intrinsic_value,
    norm_cdf,
    norm_pdf,
    to_decimal,
)

__all__ = ["AnalyticGreeks", "compute_greeks", "delta", "gamma", "theta", "vega", "rho"]

#: Calendar days per year, used to convert annual theta to daily.
DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class AnalyticGreeks:
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    rho: Decimal
    price: Decimal
    source: GreekSource = GreekSource.COMPUTED

    def to_dict(self) -> dict[str, str]:
        return {
            "delta": str(self.delta),
            "gamma": str(self.gamma),
            "theta": str(self.theta),
            "vega": str(self.vega),
            "rho": str(self.rho),
            "price": str(self.price),
            "source": self.source.value,
        }


def delta(inputs: BlackScholesInputs) -> float:
    """Rate of change of price with the underlying.

    At expiry delta collapses to 1/0 for a call and -1/0 for a put: the option is
    either stock or nothing, with no sensitivity in between.
    """
    if inputs.is_expired or inputs.volatility == 0:
        itm = intrinsic_value(inputs.spot, inputs.strike, inputs.option_type) > 0
        if inputs.option_type is OptionType.CE:
            return 1.0 if itm else 0.0
        return -1.0 if itm else 0.0

    d1, _ = d1_d2(inputs)
    carry_discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
    if inputs.option_type is OptionType.CE:
        return carry_discount * norm_cdf(d1)
    return carry_discount * (norm_cdf(d1) - 1.0)


def gamma(inputs: BlackScholesInputs) -> float:
    """Rate of change of delta. Identical for calls and puts."""
    if inputs.is_expired or inputs.volatility == 0:
        return 0.0
    d1, _ = d1_d2(inputs)
    carry_discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
    return (
        carry_discount
        * norm_pdf(d1)
        / (inputs.spot * inputs.volatility * math.sqrt(inputs.time_to_expiry))
    )


def theta(inputs: BlackScholesInputs) -> float:
    """Time decay **per calendar day** (negative for long options)."""
    if inputs.is_expired or inputs.volatility == 0:
        return 0.0

    d1, d2 = d1_d2(inputs)
    discount = math.exp(-inputs.rate * inputs.time_to_expiry)
    carry_discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
    common = -(
        inputs.spot
        * carry_discount
        * norm_pdf(d1)
        * inputs.volatility
        / (2.0 * math.sqrt(inputs.time_to_expiry))
    )

    if inputs.option_type is OptionType.CE:
        annual = (
            common
            - inputs.rate * inputs.strike * discount * norm_cdf(d2)
            + inputs.dividend_yield * inputs.spot * carry_discount * norm_cdf(d1)
        )
    else:
        annual = (
            common
            + inputs.rate * inputs.strike * discount * norm_cdf(-d2)
            - inputs.dividend_yield * inputs.spot * carry_discount * norm_cdf(-d1)
        )
    return annual / DAYS_PER_YEAR


def vega(inputs: BlackScholesInputs) -> float:
    """Sensitivity to a **1 percentage point** change in implied volatility."""
    if inputs.is_expired or inputs.volatility == 0:
        return 0.0
    d1, _ = d1_d2(inputs)
    carry_discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
    annual = inputs.spot * carry_discount * norm_pdf(d1) * math.sqrt(inputs.time_to_expiry)
    return annual / 100.0


def rho(inputs: BlackScholesInputs) -> float:
    """Sensitivity to a **1 percentage point** change in the risk-free rate."""
    if inputs.is_expired or inputs.volatility == 0:
        return 0.0
    _, d2 = d1_d2(inputs)
    discount = math.exp(-inputs.rate * inputs.time_to_expiry)
    if inputs.option_type is OptionType.CE:
        annual = inputs.strike * inputs.time_to_expiry * discount * norm_cdf(d2)
    else:
        annual = -inputs.strike * inputs.time_to_expiry * discount * norm_cdf(-d2)
    return annual / 100.0


def compute_greeks(inputs: BlackScholesInputs) -> AnalyticGreeks:
    """Every Greek plus the model price, in one pass."""
    from app.fno.greeks.black_scholes import price as bs_price

    return AnalyticGreeks(
        delta=to_decimal(delta(inputs)),
        gamma=to_decimal(gamma(inputs)),
        theta=to_decimal(theta(inputs)),
        vega=to_decimal(vega(inputs)),
        rho=to_decimal(rho(inputs)),
        price=bs_price(inputs),
        source=GreekSource.COMPUTED,
    )
