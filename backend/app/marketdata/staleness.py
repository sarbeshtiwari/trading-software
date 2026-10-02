"""Staleness detection — MD-008, GRK-008, RISK-013.

The dangerous failure in a trading system is not missing data; it is *old data
that still looks current*. A feed that stalls at 09:42 leaves every price object
in memory looking perfectly valid, and a strategy will happily size a position
against a price that stopped being true twenty minutes ago.

So freshness is explicit and checked at the point of use:

* :class:`FreshnessPolicy` holds the budget per kind of data.
* :func:`check_fresh` raises :class:`StaleDataError` — new entries stop.
* :func:`is_fresh` answers the same question without raising, for display.

Exits are deliberately *not* blocked by staleness. A stale price is a reason not
to open a position; it is not a reason to abandon one that is already open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app.core.clock import Clock, get_clock, ensure_ist
from app.core.errors import StaleDataError
from app.core.logging import get_logger

logger = get_logger("marketdata.staleness")

__all__ = ["FreshnessPolicy", "StalenessReport", "check_fresh", "is_fresh", "age_seconds"]


@dataclass(frozen=True)
class FreshnessPolicy:
    """How old each kind of input may be before it stops being actionable."""

    tick_seconds: float = 15.0
    quote_seconds: float = 15.0
    greeks_seconds: float = 60.0
    chain_seconds: float = 60.0
    portfolio_seconds: float = 30.0
    #: Bars are stale only after a full extra interval has elapsed.
    bar_grace_multiple: float = 2.0

    def budget_for(self, kind: str) -> float:
        return {
            "tick": self.tick_seconds,
            "quote": self.quote_seconds,
            "greeks": self.greeks_seconds,
            "chain": self.chain_seconds,
            "portfolio": self.portfolio_seconds,
        }.get(kind, self.quote_seconds)

    @classmethod
    def from_settings(cls, settings) -> "FreshnessPolicy":  # type: ignore[no-untyped-def]
        return cls(
            tick_seconds=float(settings.tick_staleness_seconds),
            quote_seconds=float(settings.tick_staleness_seconds),
            greeks_seconds=float(settings.greeks_staleness_seconds),
            chain_seconds=float(settings.greeks_staleness_seconds),
        )


@dataclass(frozen=True)
class StalenessReport:
    kind: str
    observed_at: Optional[datetime]
    age_seconds: Optional[float]
    budget_seconds: float
    stale: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "age_seconds": self.age_seconds,
            "budget_seconds": self.budget_seconds,
            "stale": self.stale,
            "detail": self.detail,
        }


def age_seconds(observed_at: Optional[datetime], clock: Optional[Clock] = None) -> Optional[float]:
    if observed_at is None:
        return None
    now = (clock or get_clock()).now()
    return (now - ensure_ist(observed_at)).total_seconds()


def evaluate(
    observed_at: Optional[datetime],
    *,
    kind: str = "quote",
    policy: Optional[FreshnessPolicy] = None,
    clock: Optional[Clock] = None,
    budget_seconds: Optional[float] = None,
) -> StalenessReport:
    """Report freshness without deciding what to do about it."""
    resolved_policy = policy or FreshnessPolicy()
    budget = budget_seconds if budget_seconds is not None else resolved_policy.budget_for(kind)

    if observed_at is None:
        return StalenessReport(
            kind=kind,
            observed_at=None,
            age_seconds=None,
            budget_seconds=budget,
            stale=True,
            detail="no observation timestamp",
        )

    age = age_seconds(observed_at, clock) or 0.0
    if age < 0:
        # A timestamp from the future means clock skew somewhere; treat it as
        # untrustworthy rather than as very fresh.
        return StalenessReport(
            kind=kind,
            observed_at=observed_at,
            age_seconds=age,
            budget_seconds=budget,
            stale=True,
            detail=f"observation is {abs(age):.1f}s in the future",
        )

    stale = age > budget
    return StalenessReport(
        kind=kind,
        observed_at=observed_at,
        age_seconds=age,
        budget_seconds=budget,
        stale=stale,
        detail=f"{age:.1f}s old (budget {budget:.0f}s)" if stale else "",
    )


def is_fresh(
    observed_at: Optional[datetime],
    *,
    kind: str = "quote",
    policy: Optional[FreshnessPolicy] = None,
    clock: Optional[Clock] = None,
) -> bool:
    return not evaluate(observed_at, kind=kind, policy=policy, clock=clock).stale


def check_fresh(
    observed_at: Optional[datetime],
    *,
    kind: str = "quote",
    what: str = "market data",
    policy: Optional[FreshnessPolicy] = None,
    clock: Optional[Clock] = None,
    budget_seconds: Optional[float] = None,
) -> StalenessReport:
    """Raise :class:`StaleDataError` when the data is too old to act on."""
    report = evaluate(
        observed_at, kind=kind, policy=policy, clock=clock, budget_seconds=budget_seconds
    )
    if report.stale:
        raise StaleDataError(
            f"{what} is stale: {report.detail or 'no timestamp'}",
            context=report.to_dict(),
        )
    return report


def bar_is_stale(
    bar_ts: datetime,
    interval_minutes: int,
    *,
    policy: Optional[FreshnessPolicy] = None,
    clock: Optional[Clock] = None,
) -> bool:
    """A bar is stale once more than ``grace`` intervals have passed since it closed."""
    resolved = policy or FreshnessPolicy()
    now = (clock or get_clock()).now()
    closed_at = ensure_ist(bar_ts) + timedelta(minutes=interval_minutes)
    allowance = timedelta(minutes=interval_minutes * resolved.bar_grace_multiple)
    return now - closed_at > allowance
