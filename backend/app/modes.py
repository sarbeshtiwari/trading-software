"""Trading-mode model — the single authority on which mode the system runs in.

Requirements: ARCH-001, ARCH-002, ARCH-003.

Safety contract
---------------
* ``TradingMode`` is the ONLY place mode names are defined. No other module may
  compare against raw strings such as ``"LIVE"``.
* The mode is resolved once, at startup, and is immutable for the lifetime of the
  process. Attempting to change it raises :class:`ImmutableModeError`.
* An unset or unparseable value always resolves to ``PAPER`` (never to a mode
  that can move real money).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "TradingMode",
    "ImmutableModeError",
    "parse_trading_mode",
    "resolve_mode",
    "current_mode",
    "reset_mode_for_testing",
]


class TradingMode(str, Enum):
    """The three operating modes. ``PAPER`` is always the safe default."""

    PAPER = "PAPER"
    SUPERVISED = "SUPERVISED"
    LIVE = "LIVE"

    @property
    def uses_real_broker(self) -> bool:
        """True when orders may reach a real broker in this mode."""
        return self in (TradingMode.SUPERVISED, TradingMode.LIVE)

    @property
    def requires_human_approval_per_order(self) -> bool:
        return self is TradingMode.SUPERVISED

    @property
    def requires_arming(self) -> bool:
        """LIVE additionally requires an explicit arming ceremony (LIVE-001)."""
        return self is TradingMode.LIVE


class ImmutableModeError(RuntimeError):
    """Raised on any attempt to change the trading mode after resolution."""


def parse_trading_mode(raw: Optional[str]) -> TradingMode:
    """Parse a raw configuration value into a :class:`TradingMode`.

    ARCH-003: unset, blank or unparseable values resolve to ``PAPER`` and emit a
    warning. This function never raises — failing closed to ``PAPER`` is always
    safer than refusing to boot into the safe mode.
    """
    if raw is None or not str(raw).strip():
        logger.warning(
            "TRADING_MODE is not set; defaulting to PAPER",
            extra={"resolved_mode": TradingMode.PAPER.value},
        )
        return TradingMode.PAPER

    candidate = str(raw).strip().upper()
    try:
        return TradingMode(candidate)
    except ValueError:
        logger.warning(
            "TRADING_MODE value %r is not a valid mode; defaulting to PAPER. "
            "Valid modes: %s",
            raw,
            ", ".join(m.value for m in TradingMode),
            extra={"resolved_mode": TradingMode.PAPER.value},
        )
        return TradingMode.PAPER


# --- Process-wide resolved mode (ARCH-002) --------------------------------

_resolved_mode: Optional[TradingMode] = None


def resolve_mode(mode: TradingMode) -> TradingMode:
    """Fix the process mode. Idempotent for the same value, fatal for a change.

    ARCH-002: resolving twice with a *different* mode raises
    :class:`ImmutableModeError`. Resolving twice with the same mode is allowed so
    that test setup and application startup can both call it safely.
    """
    global _resolved_mode
    if _resolved_mode is not None and _resolved_mode is not mode:
        raise ImmutableModeError(
            f"Trading mode is already resolved to {_resolved_mode.value}; "
            f"refusing to change it to {mode.value}. Restart the process to change mode."
        )
    _resolved_mode = mode
    return _resolved_mode


def current_mode() -> TradingMode:
    """Return the resolved mode, or raise if startup has not resolved one yet."""
    if _resolved_mode is None:
        raise ImmutableModeError(
            "Trading mode has not been resolved yet. "
            "Call resolve_mode() during application startup before using the mode."
        )
    return _resolved_mode


def reset_mode_for_testing() -> None:
    """Clear the resolved mode.

    Test-only escape hatch. Production code must never call this; the layering
    test (ARCH-010) asserts that nothing under ``app/`` other than this module
    references it.
    """
    global _resolved_mode
    _resolved_mode = None
