"""Portfolio Greek aggregation — GRK-006.

Net delta, gamma, theta and vega across every option and futures position.

Two details decide whether the numbers mean anything:

* **Lot size.** A delta of 0.62 on a NIFTY option is 0.62 per unit; the position
  is 75 units per lot. Aggregating unscaled deltas understates exposure by the
  lot size — which for index options is a factor of 75.
* **Sign.** A short option has the opposite Greek signs to a long one. A short
  straddle is short gamma and *long* theta, and a book that reports it as short
  theta is reporting the opposite of its actual risk.

Futures contribute delta 1 per unit and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from app.core.enums import InstrumentType, OptionType
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.marketdata.models import Greeks

logger = get_logger("fno.greeks.portfolio")

__all__ = ["GreekPosition", "PortfolioGreeks", "aggregate_greeks"]


@dataclass(frozen=True)
class GreekPosition:
    """One position, with everything needed to scale its Greeks."""

    trading_symbol: str
    instrument_type: InstrumentType
    #: Signed: positive long, negative short.
    net_quantity: int
    lot_size: int = 1
    greeks: Optional[Greeks] = None
    spot: Optional[Decimal] = None
    option_type: Optional[OptionType] = None

    @property
    def units(self) -> int:
        """Signed unit count. Quantities are already in units, not lots."""
        return self.net_quantity

    @property
    def is_option(self) -> bool:
        return self.instrument_type is InstrumentType.OPTION

    @property
    def is_future(self) -> bool:
        return self.instrument_type is InstrumentType.FUTURE


@dataclass
class PortfolioGreeks:
    delta: Decimal = Decimal(0)
    gamma: Decimal = Decimal(0)
    theta: Decimal = Decimal(0)
    vega: Decimal = Decimal(0)
    rho: Decimal = Decimal(0)
    #: Delta expressed in currency: delta x spot, summed.
    delta_notional: Decimal = Decimal(0)
    positions_included: int = 0
    positions_missing_greeks: list[str] = field(default_factory=list)

    @property
    def is_long_gamma(self) -> bool:
        return self.gamma > 0

    @property
    def is_long_theta(self) -> bool:
        """Positive theta means time decay works in the book's favour."""
        return self.theta > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "delta": str(self.delta),
            "gamma": str(self.gamma),
            "theta": str(self.theta),
            "vega": str(self.vega),
            "rho": str(self.rho),
            "delta_notional": str(self.delta_notional),
            "positions_included": self.positions_included,
            "positions_missing_greeks": list(self.positions_missing_greeks),
        }


def aggregate_greeks(positions: Iterable[GreekPosition]) -> PortfolioGreeks:
    """Sum position Greeks, scaled by signed quantity.

    Positions whose Greeks are unavailable are **listed, not skipped silently**:
    a portfolio delta computed from half the book is worse than no number,
    because it looks like a complete answer.
    """
    total = PortfolioGreeks()

    for position in positions:
        if position.is_future:
            # A future is delta-one per unit; gamma, theta and vega are zero.
            contribution = Decimal(position.units)
            total.delta += contribution
            if position.spot is not None:
                total.delta_notional += contribution * position.spot
            total.positions_included += 1
            continue

        if not position.is_option:
            continue

        greeks = position.greeks
        if greeks is None or greeks.delta is None:
            total.positions_missing_greeks.append(position.trading_symbol)
            continue

        units = Decimal(position.units)
        total.delta += (greeks.delta or Decimal(0)) * units
        total.gamma += (greeks.gamma or Decimal(0)) * units
        total.theta += (greeks.theta or Decimal(0)) * units
        total.vega += (greeks.vega or Decimal(0)) * units
        total.rho += (greeks.rho or Decimal(0)) * units

        if position.spot is not None and greeks.delta is not None:
            total.delta_notional += greeks.delta * units * position.spot

        total.positions_included += 1

    total.delta_notional = quantize_money(total.delta_notional)

    if total.positions_missing_greeks:
        logger.warning(
            "Portfolio Greeks are incomplete",
            extra={
                "missing": total.positions_missing_greeks,
                "included": total.positions_included,
            },
        )
    return total
