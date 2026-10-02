"""Pricing assumptions — GRK-005.

The risk-free rate and dividend yield are inputs to every Greek, and they are
assumptions rather than observations. They are therefore configurable, versioned,
and recorded alongside every computed Greek: a delta computed at a 6.5% rate is
not the same number as one computed at 7%, and six months later nobody will
remember which was used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.core.clock import Clock, get_clock

__all__ = ["PricingAssumptions", "default_assumptions", "set_default_assumptions"]


@dataclass(frozen=True)
class PricingAssumptions:
    """Rate and yield assumptions, with provenance.

    ``rate`` and ``dividend_yield`` are annualised decimals: 0.065 means 6.5%.
    """

    rate: float = 0.065
    dividend_yield: float = 0.0
    version: str = "1.0.0"
    source: str = "default"
    note: str = "Indian risk-free proxy; review against the prevailing T-bill yield"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "dividend_yield": self.dividend_yield,
            "version": self.version,
            "source": self.source,
        }

    def stamp(self, clock: Optional[Clock] = None) -> dict[str, Any]:
        """Assumptions plus the moment they were applied, for the audit record."""
        payload = self.to_dict()
        payload["applied_at"] = (clock or get_clock()).now().isoformat()
        return payload


_defaults = PricingAssumptions()


def default_assumptions() -> PricingAssumptions:
    return _defaults


def set_default_assumptions(assumptions: PricingAssumptions) -> PricingAssumptions:
    """Replace the process defaults. Startup configuration and tests only."""
    global _defaults
    _defaults = assumptions
    return _defaults
