"""Greek source resolution — GRK-004, GRK-008.

The broker supplies Greeks; so does the local model. They will not always agree,
and the disagreement is informative rather than a nuisance:

* **Broker Greeks win** when they are present and fresh. They reflect the same
  model the exchange and the rest of the market are looking at.
* **Computed Greeks fill the gaps**, labelled ``COMPUTED`` so nothing downstream
  mistakes a model output for a market observation.
* **Material divergence raises a data-quality event.** If the broker's delta and
  ours differ by more than a tolerance, one of them is wrong, and acting on
  either without noticing is worse than acting on neither.

Stale Greeks are refused outright (GRK-008): an option's Greeks a minute old are
a different option's Greeks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.core.clock import Clock, get_clock
from app.core.enums import GreekSource, OptionType
from app.core.errors import StaleDataError
from app.core.logging import get_logger
from app.fno.greeks.analytic import compute_greeks
from app.fno.greeks.black_scholes import BlackScholesInputs
from app.fno.greeks.params import PricingAssumptions, default_assumptions
from app.fno.greeks.time import calendar_years_to_expiry
from app.marketdata.models import Greeks
from app.marketdata.staleness import FreshnessPolicy, evaluate

logger = get_logger("fno.greeks.resolver")

__all__ = ["GreekDivergence", "ResolvedGreeks", "GreeksResolver"]

#: Absolute tolerances before broker and model are considered to disagree.
DELTA_TOLERANCE = Decimal("0.05")
IV_TOLERANCE = Decimal("2.0")  # percentage points


@dataclass(frozen=True)
class GreekDivergence:
    field: str
    broker: Decimal
    computed: Decimal

    @property
    def difference(self) -> Decimal:
        return abs(self.broker - self.computed)

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "broker": str(self.broker),
            "computed": str(self.computed),
            "difference": str(self.difference),
        }


@dataclass(frozen=True)
class ResolvedGreeks:
    greeks: Greeks
    source: GreekSource
    divergences: tuple[GreekDivergence, ...] = ()
    assumptions: Optional[dict] = None

    @property
    def has_divergence(self) -> bool:
        return bool(self.divergences)


class GreeksResolver:
    def __init__(
        self,
        *,
        assumptions: Optional[PricingAssumptions] = None,
        freshness: Optional[FreshnessPolicy] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self._assumptions = assumptions or default_assumptions()
        self._freshness = freshness or FreshnessPolicy()
        self._clock = clock or get_clock()

    def compute(
        self,
        *,
        spot: float,
        strike: float,
        expiry: date,
        option_type: OptionType,
        volatility: float,
        now: Optional[datetime] = None,
    ) -> Greeks:
        """Model Greeks, stamped with the assumptions used."""
        years = calendar_years_to_expiry(expiry, now=now, clock=self._clock)
        inputs = BlackScholesInputs(
            spot=spot,
            strike=strike,
            time_to_expiry=years,
            volatility=volatility,
            rate=self._assumptions.rate,
            dividend_yield=self._assumptions.dividend_yield,
            option_type=option_type,
        )
        analytic = compute_greeks(inputs)
        return Greeks(
            delta=analytic.delta,
            gamma=analytic.gamma,
            theta=analytic.theta,
            vega=analytic.vega,
            rho=analytic.rho,
            implied_volatility=Decimal(str(volatility * 100)),
            source=GreekSource.COMPUTED,
            computed_at=self._clock.now(),
            assumptions=self._assumptions.stamp(self._clock),
        )

    def resolve(
        self,
        *,
        broker_greeks: Optional[Greeks],
        spot: float,
        strike: float,
        expiry: date,
        option_type: OptionType,
        fallback_volatility: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> ResolvedGreeks:
        """Prefer broker Greeks; compute where they are missing; flag disagreement."""
        computed: Optional[Greeks] = None
        volatility = fallback_volatility

        if broker_greeks is not None and broker_greeks.implied_volatility is not None:
            volatility = float(broker_greeks.implied_volatility) / 100.0

        if volatility is not None and volatility > 0:
            computed = self.compute(
                spot=spot,
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                volatility=volatility,
                now=now,
            )

        if broker_greeks is None:
            if computed is None:
                # Neither source can produce a value. Saying so beats inventing one.
                return ResolvedGreeks(
                    greeks=Greeks(source=GreekSource.COMPUTED, computed_at=self._clock.now()),
                    source=GreekSource.COMPUTED,
                    assumptions=self._assumptions.to_dict(),
                )
            return ResolvedGreeks(
                greeks=computed,
                source=GreekSource.COMPUTED,
                assumptions=self._assumptions.to_dict(),
            )

        divergences = self._compare(broker_greeks, computed)
        if divergences:
            logger.warning(
                "Broker and model Greeks disagree",
                extra={
                    "strike": strike,
                    "option_type": option_type.value,
                    "divergences": [item.to_dict() for item in divergences],
                },
            )

        return ResolvedGreeks(
            greeks=broker_greeks,
            source=GreekSource.BROKER,
            divergences=tuple(divergences),
            assumptions=self._assumptions.to_dict(),
        )

    def require_fresh(self, greeks: Greeks, *, what: str = "greeks") -> None:
        """Raise when Greeks are too old to act on (GRK-008)."""
        report = evaluate(
            greeks.computed_at, kind="greeks", policy=self._freshness, clock=self._clock
        )
        if report.stale:
            raise StaleDataError(
                f"{what} are stale: {report.detail or 'no timestamp'}",
                context=report.to_dict(),
            )

    def _compare(
        self, broker: Greeks, computed: Optional[Greeks]
    ) -> list[GreekDivergence]:
        if computed is None:
            return []
        divergences: list[GreekDivergence] = []

        if broker.delta is not None and computed.delta is not None:
            if abs(broker.delta - computed.delta) > DELTA_TOLERANCE:
                divergences.append(
                    GreekDivergence("delta", broker.delta, computed.delta)
                )
        if broker.implied_volatility is not None and computed.implied_volatility is not None:
            if abs(broker.implied_volatility - computed.implied_volatility) > IV_TOLERANCE:
                divergences.append(
                    GreekDivergence(
                        "implied_volatility",
                        broker.implied_volatility,
                        computed.implied_volatility,
                    )
                )
        return divergences
