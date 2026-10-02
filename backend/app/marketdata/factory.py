"""Market-data provider selection — ARCH-006 (data side), PAPER-002.

Paper mode uses **live** market data by default: the point of paper trading is to
behave like the real thing everywhere except execution. Replay is opt-in and only
selectable explicitly, never as a silent fallback when live data is unavailable —
falling back to recorded data without saying so would make a paper session look
successful on prices that never happened today.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

from app.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.marketdata.base import MarketDataProvider

logger = get_logger("marketdata.factory")

__all__ = [
    "MarketDataProviderName",
    "register_market_data_provider",
    "unregister_market_data_provider",
    "available_market_data_providers",
    "create_market_data_provider",
]


class MarketDataProviderName(str, Enum):
    LIVE = "live"
    HISTORICAL = "historical"
    REPLAY = "replay"


MarketDataFactory = Callable[[Settings], MarketDataProvider]

_registry: dict[MarketDataProviderName, MarketDataFactory] = {}


def register_market_data_provider(
    name: MarketDataProviderName, factory: MarketDataFactory
) -> None:
    _registry[name] = factory
    logger.debug("Registered market-data provider", extra={"provider": name.value})


def unregister_market_data_provider(name: MarketDataProviderName) -> None:
    """Test helper: remove a registration."""
    _registry.pop(name, None)


def available_market_data_providers() -> frozenset[MarketDataProviderName]:
    return frozenset(_registry)


def create_market_data_provider(
    settings: Optional[Settings] = None,
    *,
    name: MarketDataProviderName = MarketDataProviderName.LIVE,
) -> MarketDataProvider:
    """Build a market-data provider.

    ``REPLAY`` is refused in any mode that can reach a real broker: replayed bars
    driving live orders is precisely the failure ARCH-008 exists to prevent.
    """
    resolved = settings or get_settings()

    if name is MarketDataProviderName.REPLAY and resolved.trading_mode.uses_real_broker:
        raise ConfigurationError(
            f"Replay market data cannot be used in {resolved.trading_mode.value} mode.",
            context={"trading_mode": resolved.trading_mode.value},
        )

    factory = _registry.get(name)
    if factory is None:
        raise ConfigurationError(
            f"Market-data provider {name.value!r} is not registered. "
            f"Registered providers: {sorted(p.value for p in _registry) or 'none'}.",
            context={"requested": name.value},
        )

    provider = factory(resolved)
    logger.info(
        "Market-data provider selected",
        extra={"provider": name.value, "data_origin": provider.data_origin.value},
    )
    return provider
