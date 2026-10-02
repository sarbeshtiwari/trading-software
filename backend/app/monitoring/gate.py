"""Trading gate — MON-004.

A single place that answers "may the system trade right now, and if not, why
not". Everything that could stop trading funnels through here: failing critical
health checks, the kill switch, emergency controls, an unresolved reconciliation
discrepancy, or LIVE mode that has not been armed.

The gate is deliberately **fail-closed**: it starts disabled and only opens when
something explicitly says it may.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.core.clock import get_clock
from app.core.errors import TradingDisabledError
from app.core.logging import get_logger

logger = get_logger("monitoring.gate")

__all__ = ["GateState", "TradingGate", "get_trading_gate", "reset_trading_gate"]


@dataclass(frozen=True)
class GateState:
    trading_enabled: bool
    new_entries_allowed: bool
    reasons: tuple[str, ...] = ()
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "trading_enabled": self.trading_enabled,
            "new_entries_allowed": self.new_entries_allowed,
            "reasons": list(self.reasons),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class _Blocker:
    source: str
    reason: str
    blocks_exits: bool = False
    since: Optional[datetime] = None


class TradingGate:
    """Aggregates every reason trading might be blocked."""

    def __init__(self) -> None:
        self._blockers: dict[str, _Blocker] = {}
        # Fail closed until startup explicitly clears the initial blocker.
        self._blockers["startup"] = _Blocker(
            source="startup",
            reason="startup health checks have not completed",
            blocks_exits=True,
        )

    # --- Mutation ---------------------------------------------------------

    def block(self, source: str, reason: str, *, blocks_exits: bool = False) -> None:
        """Record a reason trading is blocked.

        ``blocks_exits=False`` means new entries stop but existing positions can
        still be managed and closed — the right behaviour for a daily-loss breach.
        ``blocks_exits=True`` stops everything, which is only correct when the
        system cannot trust its own state.
        """
        existing = self._blockers.get(source)
        if existing and existing.reason == reason and existing.blocks_exits == blocks_exits:
            return
        self._blockers[source] = _Blocker(
            source=source, reason=reason, blocks_exits=blocks_exits, since=get_clock().now()
        )
        logger.warning(
            "Trading blocked",
            extra={"source": source, "reason": reason, "blocks_exits": blocks_exits},
        )

    def clear(self, source: str) -> None:
        """Remove one blocker. Others remain in force."""
        if self._blockers.pop(source, None) is not None:
            logger.info("Trading blocker cleared", extra={"source": source})

    # --- Queries ----------------------------------------------------------

    @property
    def state(self) -> GateState:
        reasons = tuple(f"{b.source}: {b.reason}" for b in self._blockers.values())
        blocks_everything = any(b.blocks_exits for b in self._blockers.values())
        return GateState(
            trading_enabled=not blocks_everything,
            new_entries_allowed=not self._blockers,
            reasons=reasons,
            updated_at=get_clock().now(),
        )

    @property
    def new_entries_allowed(self) -> bool:
        return not self._blockers

    @property
    def trading_enabled(self) -> bool:
        return not any(b.blocks_exits for b in self._blockers.values())

    def reason(self) -> Optional[str]:
        if not self._blockers:
            return None
        return "; ".join(f"{b.source}: {b.reason}" for b in self._blockers.values())

    # --- Enforcement ------------------------------------------------------

    def require_new_entries_allowed(self) -> None:
        """Raise unless new entries are permitted right now."""
        if not self.new_entries_allowed:
            raise TradingDisabledError(
                f"New entries are blocked: {self.reason()}",
                context={"reasons": list(self.state.reasons)},
            )

    def require_trading_enabled(self) -> None:
        """Raise unless any trading action (including exits) is permitted."""
        if not self.trading_enabled:
            raise TradingDisabledError(
                f"Trading is disabled: {self.reason()}",
                context={"reasons": list(self.state.reasons)},
            )


_gate = TradingGate()


def get_trading_gate() -> TradingGate:
    return _gate


def reset_trading_gate() -> TradingGate:
    """Test helper: fresh, fail-closed gate."""
    global _gate
    _gate = TradingGate()
    return _gate
