"""Broker and market-data health checks — MON-002, AUTH-004, AUTH-006.

These cover the contract §10 entries whose subjects exist after P1: Groww
authentication, account, margin, positions, instruments, market data and market
status.

Criticality is **mode-dependent**, and that is the point. In PAPER mode the
system does not need Groww credentials at all, so a missing credential is
``SKIPPED`` rather than a failure — reporting it as a failure would train the
operator to ignore red checks. In SUPERVISED or LIVE the same condition is
critical and disables trading.
"""

from __future__ import annotations

from typing import Any, Optional

from app.brokers.base import BrokerProvider
from app.config import Settings, get_settings
from app.core.clock import Clock, get_clock
from app.core.data_origin import ExecutionRealism
from app.core.enums import HealthStatus, Segment
from app.core.logging import get_logger
from app.core.sessions import phase_at
from app.instruments.resolver import InstrumentResolver, get_resolver
from app.monitoring.healthchecks import HealthCheck

logger = get_logger("monitoring.broker_checks")

__all__ = [
    "GrowwAuthCheck",
    "AccountCheck",
    "MarginCheck",
    "PositionsCheck",
    "InstrumentsCheck",
    "MarketDataCheck",
    "MarketStatusCheck",
    "register_broker_checks",
]


class _BrokerCheck(HealthCheck):
    """Shared plumbing: mode-dependent criticality and a broker handle."""

    def __init__(
        self,
        broker: Optional[BrokerProvider] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._broker = broker
        self.critical = self._settings.trading_mode.uses_real_broker

    def _skip_reason(self) -> Optional[str]:
        if not self._settings.trading_mode.uses_real_broker:
            if not self._settings.has_groww_credentials:
                return (
                    f"{self._settings.trading_mode.value} mode does not require Groww "
                    f"credentials"
                )
        if self._broker is None:
            return "no broker provider is wired into the health registry"
        return None


class GrowwAuthCheck(_BrokerCheck):
    """AUTH-004/AUTH-006: credentials must actually work before trading."""

    name = "groww_auth"
    timeout_seconds = 10.0

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        if not self._settings.has_groww_credentials:
            if self._settings.trading_mode.uses_real_broker:
                return (
                    HealthStatus.FAIL,
                    f"{self._settings.trading_mode.value} mode requires Groww credentials",
                    {},
                )
            return HealthStatus.SKIPPED, "no credentials configured (PAPER mode)", {}

        if self._broker is None:
            return HealthStatus.SKIPPED, "no broker provider wired", {}

        if (
            self._broker.name != "groww"
            or self._broker.execution_realism is not ExecutionRealism.REAL
        ):
            return (
                HealthStatus.FAIL if self.critical else HealthStatus.SKIPPED,
                "Groww authentication UNVERIFIED: selected adapter is not real Groww",
                {},
            )

        reachable = await self._broker.ping()
        context = {"auth_flow": self._settings.groww_auth_flow}
        if not reachable:
            return HealthStatus.FAIL, "authenticated call to Groww failed", context
        return (
            HealthStatus.PASS,
            "Groww read-only connectivity verified; live execution UNVERIFIED",
            context,
        )


class AccountCheck(_BrokerCheck):
    name = "account"
    timeout_seconds = 10.0

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        skip = self._skip_reason()
        if skip:
            return HealthStatus.SKIPPED, skip, {}
        profile = await self._broker.get_profile()  # type: ignore[union-attr]
        return (
            HealthStatus.PASS,
            f"account {profile.account_id}",
            {"account_id": profile.account_id},
        )


class MarginCheck(_BrokerCheck):
    """Available margin is a hard input to sizing and to the risk engine."""

    name = "margin"
    timeout_seconds = 10.0

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        skip = self._skip_reason()
        if skip:
            return HealthStatus.SKIPPED, skip, {}

        margin = await self._broker.get_margin()  # type: ignore[union-attr]
        context = {
            "available_margin": str(margin.available_margin),
            "source": margin.source.value,
        }
        if margin.available_margin <= 0:
            # Not a failure: an account can legitimately be fully deployed. But
            # nothing new can be opened, so it must be visible.
            return HealthStatus.DEGRADED, "no available margin", context
        return HealthStatus.PASS, f"{margin.available_margin} available", context


class PositionsCheck(_BrokerCheck):
    """The positions call must work before trading; reconciliation depends on it."""

    name = "positions"
    timeout_seconds = 15.0

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        skip = self._skip_reason()
        if skip:
            return HealthStatus.SKIPPED, skip, {}

        positions = await self._broker.get_positions()  # type: ignore[union-attr]
        open_positions = [p for p in positions if p.net_quantity != 0]
        return (
            HealthStatus.PASS,
            f"{len(open_positions)} open position(s)",
            {"total": len(positions), "open": len(open_positions)},
        )


class InstrumentsCheck(HealthCheck):
    """An empty instrument master means no lot sizes and no tick sizes."""

    name = "instruments"
    critical = True
    timeout_seconds = 5.0

    def __init__(self, resolver: Optional[InstrumentResolver] = None) -> None:
        self._resolver = resolver or get_resolver()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        # Always re-read rather than trusting the cached index: this check exists
        # to answer whether the instrument master is loaded *now*. A stale cache
        # reporting PASS after the table was emptied is exactly the failure the
        # check is supposed to catch.
        await self._resolver.refresh()

        size = self._resolver.size
        if size == 0:
            return (
                HealthStatus.FAIL,
                "instrument master is empty; lot and tick sizes are unknown",
                {"instruments": 0},
            )
        return HealthStatus.PASS, f"{size} instruments", {"instruments": size}


class MarketDataCheck(HealthCheck):
    """Market data must be flowing, and recently."""

    name = "market_data"
    critical = True
    timeout_seconds = 10.0

    def __init__(
        self,
        provider: Optional[Any] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        if self._provider is None:
            return HealthStatus.SKIPPED, "no market-data provider wired", {}

        status = getattr(self._provider, "status", HealthStatus.SKIPPED)
        origin = self._provider.data_origin
        context = {"provider": getattr(self._provider, "name", "unknown"),
                   "data_origin": origin.value}

        if status is HealthStatus.DEGRADED:
            # MD-010: REST polling still serves prices; it is a degradation.
            return (
                HealthStatus.DEGRADED,
                "running on REST polling; the websocket feed is unavailable",
                context,
            )
        if status is HealthStatus.PASS:
            return HealthStatus.PASS, "market data available", context
        if status is HealthStatus.SKIPPED:
            return HealthStatus.SKIPPED, "market-data health evidence unavailable", context
        return HealthStatus.FAIL, "market-data provider failed or reported invalid health", context


class MarketStatusCheck(HealthCheck):
    """Which session phase we are in, and whether the calendar can be trusted."""

    name = "market_status"
    critical = False
    timeout_seconds = 5.0

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self._clock = clock or get_clock()

    async def run(self) -> tuple[HealthStatus, str, dict[str, Any]]:
        from app.core.calendar import get_trading_calendar

        calendar = get_trading_calendar()
        now = self._clock.now()
        phase = phase_at(now, segment=Segment.CASH, calendar=calendar, clock=self._clock)
        context = {
            "phase": phase.value,
            "trading_day": calendar.is_trading_day(now.date()),
            "server_time_ist": now.isoformat(),
        }

        warning = calendar.session_warning(now.date()) or calendar.completeness_warning(now.year)
        if warning:
            # A missing holiday makes the system think a closed market is open.
            return HealthStatus.DEGRADED, warning, context

        return HealthStatus.PASS, f"market is {phase.value}", context


def register_broker_checks(
    *,
    broker: Optional[BrokerProvider] = None,
    market_data: Optional[Any] = None,
    resolver: Optional[InstrumentResolver] = None,
    settings: Optional[Settings] = None,
) -> None:
    """Register every check whose subject now exists."""
    from app.monitoring.healthchecks import get_health_registry

    resolved = settings or get_settings()
    registry = get_health_registry()

    registry.register(GrowwAuthCheck(broker, resolved))
    registry.register(AccountCheck(broker, resolved))
    registry.register(MarginCheck(broker, resolved))
    registry.register(PositionsCheck(broker, resolved))
    registry.register(InstrumentsCheck(resolver))
    registry.register(MarketDataCheck(market_data, resolved))
    registry.register(MarketStatusCheck())
