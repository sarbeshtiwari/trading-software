"""Implied volatility solver — GRK-003.

Newton-Raphson with a bisection fallback and explicit bracketing.

The contract that matters: **non-convergence returns ``None`` with a reason, never
a number**. An IV solver that quietly returns its last iterate, or zero, or the
initial guess, feeds a fabricated volatility into vega, into the skew curve, and
into every options decision downstream. "I could not solve this" is a useful
answer; a wrong number is not.

Common genuine causes of non-convergence, all of which produce ``None`` here:
a market price below the no-arbitrage floor (stale or crossed quote), a price at
or above the theoretical maximum, and zero time to expiry.

Note that the floor is the **discounted** bound, not undiscounted intrinsic
value: a deep in-the-money European put legitimately trades below intrinsic,
because it cannot be exercised early.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.core.enums import OptionType
from app.core.logging import get_logger
from app.fno.greeks.analytic import vega
from app.fno.greeks.black_scholes import BlackScholesInputs, intrinsic_value, price, to_decimal

logger = get_logger("fno.greeks.iv")

__all__ = ["IVResult", "implied_volatility"]

#: Search bounds. 1% to 500% covers every Indian option that trades.
MIN_VOL = 0.01
MAX_VOL = 5.0


@dataclass(frozen=True)
class IVResult:
    """The outcome of a solve. ``value`` is ``None`` unless it converged."""

    value: Optional[Decimal]
    converged: bool
    iterations: int
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.converged and self.value is not None


def implied_volatility(
    *,
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    option_type: OptionType,
    rate: float = 0.065,
    dividend_yield: float = 0.0,
    tolerance: float = 1e-6,
    max_iterations: int = 100,
) -> IVResult:
    """Solve for the volatility that reproduces ``market_price``."""
    if time_to_expiry <= 0:
        return IVResult(None, False, 0, "expired: no time value to imply volatility from")
    if market_price <= 0:
        return IVResult(None, False, 0, f"market price must be positive, got {market_price}")

    # The no-arbitrage floor for a EUROPEAN option is the *discounted* bound, not
    # undiscounted intrinsic value. A deep in-the-money European put legitimately
    # trades below intrinsic — it cannot be exercised early, so the present value
    # of the strike is what matters. Checking against undiscounted intrinsic would
    # reject perfectly valid quotes.
    discount = math.exp(-rate * time_to_expiry)
    carry_discount = math.exp(-dividend_yield * time_to_expiry)
    if option_type is OptionType.CE:
        floor = max(0.0, spot * carry_discount - strike * discount)
    else:
        floor = max(0.0, strike * discount - spot * carry_discount)

    if market_price < floor - tolerance:
        return IVResult(
            None,
            False,
            0,
            f"market price {market_price} is below the no-arbitrage floor "
            f"{floor:.4f}; the quote is stale or crossed",
        )

    def theoretical(volatility: float) -> float:
        return float(
            price(
                BlackScholesInputs(
                    spot=spot,
                    strike=strike,
                    time_to_expiry=time_to_expiry,
                    volatility=volatility,
                    rate=rate,
                    dividend_yield=dividend_yield,
                    option_type=option_type,
                )
            )
        )

    low_price = theoretical(MIN_VOL)
    high_price = theoretical(MAX_VOL)
    if market_price > high_price:
        return IVResult(
            None,
            False,
            0,
            f"market price {market_price} exceeds the model maximum {high_price:.4f} "
            f"at {MAX_VOL * 100:.0f}% volatility",
        )
    if market_price < low_price:
        return IVResult(
            None,
            False,
            0,
            f"market price {market_price} is below the model minimum {low_price:.4f} "
            f"at {MIN_VOL * 100:.0f}% volatility",
        )

    # Newton-Raphson from a reasonable seed, with bisection bounds carried along
    # so a bad derivative step cannot run away.
    volatility = _seed(market_price, spot, strike, time_to_expiry)
    low, high = MIN_VOL, MAX_VOL

    for iteration in range(1, max_iterations + 1):
        inputs = BlackScholesInputs(
            spot=spot,
            strike=strike,
            time_to_expiry=time_to_expiry,
            volatility=volatility,
            rate=rate,
            dividend_yield=dividend_yield,
            option_type=option_type,
        )
        model = float(price(inputs))
        difference = model - market_price

        if abs(difference) < tolerance:
            return IVResult(to_decimal(volatility * 100, "0.0001"), True, iteration)

        if difference > 0:
            high = volatility
        else:
            low = volatility

        # vega() is per volatility point; the solver works in raw sigma.
        slope = vega(inputs) * 100.0
        if slope > 1e-10:
            step = volatility - difference / slope
            volatility = step if low < step < high else (low + high) / 2.0
        else:
            volatility = (low + high) / 2.0

        if high - low < 1e-10:
            break

    logger.info(
        "IV did not converge",
        extra={
            "strike": strike,
            "spot": spot,
            "market_price": market_price,
            "last_volatility": volatility,
        },
    )
    return IVResult(
        None,
        False,
        max_iterations,
        f"did not converge within {max_iterations} iterations "
        f"(last estimate {volatility * 100:.2f}%)",
    )


def _seed(market_price: float, spot: float, strike: float, time_to_expiry: float) -> float:
    """Brenner-Subrahmanyam approximation, clamped into the search range.

    Good near the money and harmless elsewhere, because the solver brackets.
    """
    try:
        estimate = (
            math.sqrt(2.0 * math.pi / time_to_expiry) * market_price / spot
            if spot > 0 and time_to_expiry > 0
            else 0.3
        )
    except (ValueError, ZeroDivisionError):  # pragma: no cover - guarded above
        estimate = 0.3
    return min(max(estimate, MIN_VOL * 2), MAX_VOL / 2)
