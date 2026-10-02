"""Broker selection — ARCH-006, GRW-027, LIVE-010.

Nothing in the engine constructs a concrete broker. Selection happens here, from
configuration, so switching from paper to real Groww is one environment variable
and a restart rather than a code change.

Providers register themselves at import time, which keeps this module free of
imports from the implementations it selects (and keeps the layering test happy).
"""

from __future__ import annotations

from typing import Callable, Optional

from app.brokers.base import BrokerProvider
from app.config import BrokerProviderName, Settings, get_settings
from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.modes import TradingMode

logger = get_logger("brokers.factory")

__all__ = [
    "register_broker_provider",
    "unregister_broker_provider",
    "available_broker_providers",
    "resolve_broker_provider_name",
    "create_broker_provider",
]

BrokerFactory = Callable[[Settings], BrokerProvider]

_registry: dict[BrokerProviderName, BrokerFactory] = {}


def register_broker_provider(name: BrokerProviderName, factory: BrokerFactory) -> None:
    """Register a provider implementation. Called at import time by each adapter."""
    _registry[name] = factory
    logger.debug("Registered broker provider", extra={"provider": name.value})


def unregister_broker_provider(name: BrokerProviderName) -> None:
    """Test helper: remove a registration."""
    _registry.pop(name, None)


def available_broker_providers() -> frozenset[BrokerProviderName]:
    return frozenset(_registry)


def resolve_broker_provider_name(settings: Optional[Settings] = None) -> BrokerProviderName:
    """Decide which provider the current configuration implies.

    The rules, in order:

    1. ``SUPERVISED``/``LIVE`` require a real broker. A paper broker there is a
       configuration error, already rejected at settings validation (LIVE-010);
       re-checked here because this function is also called from recovery paths.
    2. ``PAPER`` always executes through the paper broker, even when Groww
       credentials are configured — in paper mode Groww is a data source, never
       an execution venue.
    """
    resolved = settings or get_settings()

    if resolved.trading_mode.uses_real_broker:
        if resolved.broker_provider is BrokerProviderName.PAPER:
            raise ConfigurationError(
                f"TRADING_MODE={resolved.trading_mode.value} cannot execute through the "
                f"paper broker.",
                context={"trading_mode": resolved.trading_mode.value},
            )
        return resolved.broker_provider

    if resolved.trading_mode is TradingMode.PAPER:
        return BrokerProviderName.PAPER

    raise ConfigurationError(  # pragma: no cover - unreachable while TradingMode has 3 members
        f"Unhandled trading mode {resolved.trading_mode!r}"
    )


def create_broker_provider(settings: Optional[Settings] = None) -> BrokerProvider:
    """Build the broker provider for the current configuration."""
    resolved = settings or get_settings()
    name = resolve_broker_provider_name(resolved)

    factory = _registry.get(name)
    if factory is None:
        raise ConfigurationError(
            f"Broker provider {name.value!r} is not registered. "
            f"Registered providers: {sorted(p.value for p in _registry) or 'none'}.",
            context={"requested": name.value},
        )

    provider = factory(resolved)
    logger.info(
        "Broker provider selected",
        extra={
            "provider": name.value,
            "trading_mode": resolved.trading_mode.value,
            "execution_realism": provider.execution_realism.value,
        },
    )
    return provider
