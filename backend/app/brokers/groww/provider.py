"""GrowwBrokerProvider — the real broker behind the ``BrokerProvider`` interface.

Nothing here contains trading logic. It composes the endpoint modules and
presents them as the interface the engine knows, so switching between paper and
Groww is a configuration change (GRW-027).

Registration happens at import time, which is why importing
``app.brokers.groww`` is enough to make ``BROKER_PROVIDER=groww`` resolvable.
"""

from __future__ import annotations

from typing import Optional, Sequence

import httpx

from app.brokers.base import BrokerProvider
from app.brokers.groww.auth import GrowwAuthenticator
from app.brokers.groww.capabilities import GROWW_CAPABILITIES
from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.margin import GrowwMarginApi
from app.brokers.groww.mapping import parse_profile
from app.brokers.groww.marketdata import GrowwMarketDataApi
from app.brokers.groww.historical import GrowwHistoricalApi
from app.brokers.groww.options import GrowwOptionsApi
from app.brokers.groww.orders import GrowwOrdersApi
from app.brokers.groww.portfolio import GrowwPortfolioApi
from app.brokers.groww.ratelimit import RateCategory
from app.brokers.models import (
    BrokerCapabilities,
    BrokerHolding,
    BrokerOrder,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    MarginInfo,
    ModifyRequest,
    OrderAck,
    OrderRequest,
)
from app.config import BrokerProviderName, Settings, get_settings
from app.core.data_origin import ExecutionRealism
from app.core.enums import Segment
from app.core.errors import ConfigurationError
from app.core.logging import get_logger

logger = get_logger("brokers.groww.provider")

__all__ = ["GrowwBrokerProvider"]


class GrowwBrokerProvider(BrokerProvider):
    """Real-money broker adapter. Every fill it reports actually happened."""

    name = "groww"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        client: Optional[GrowwClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or GrowwClient(self._settings, transport=transport)
        self.orders_api = GrowwOrdersApi(self._client)
        self.portfolio_api = GrowwPortfolioApi(self._client)
        self.margin_api = GrowwMarginApi(self._client)
        self.market_data_api = GrowwMarketDataApi(self._client)
        self.historical_api = GrowwHistoricalApi(self._client)
        self.options_api = GrowwOptionsApi(self._client)
        self._connected = False

    # --- Identity ---------------------------------------------------------

    @property
    def execution_realism(self) -> ExecutionRealism:
        return ExecutionRealism.REAL

    @property
    def capabilities(self) -> BrokerCapabilities:
        return GROWW_CAPABILITIES

    @property
    def client(self) -> GrowwClient:
        return self._client

    @property
    def authenticator(self) -> GrowwAuthenticator:
        return self._client.authenticator

    # --- Connection -------------------------------------------------------

    async def connect(self) -> None:
        """Authenticate. Idempotent: a cached token is reused."""
        if not self._settings.has_groww_credentials:
            raise ConfigurationError(
                "Groww credentials are not configured. Set GROWW_TOTP_TOKEN + "
                "GROWW_TOTP_SECRET (preferred) or GROWW_API_KEY + GROWW_API_SECRET.",
            )
        await self._client.authenticator.get_token()
        self._connected = True
        logger.info(
            "Connected to Groww",
            extra={"auth_flow": self._settings.groww_auth_flow},
        )

    async def close(self) -> None:
        await self._client.aclose()
        self._connected = False

    async def ping(self) -> bool:
        return await self._client.ping()

    async def get_profile(self) -> BrokerProfile:
        payload = await self._client.get(
            Endpoints.MARGIN.resolve(), category=RateCategory.NON_TRADING
        )
        # The margin endpoint is the cheapest authenticated read that identifies
        # the account; if it ever stops carrying an id, this raises rather than
        # inventing one.
        return parse_profile(payload if isinstance(payload, dict) else {})

    # --- Orders -----------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderAck:
        return await self.orders_api.place(request)

    async def modify_order(self, request: ModifyRequest) -> OrderAck:
        return await self.orders_api.modify(request)

    async def cancel_order(self, broker_order_id: str, segment: Segment) -> OrderAck:
        return await self.orders_api.cancel(broker_order_id, segment)

    async def get_order(self, broker_order_id: str, segment: Segment) -> BrokerOrder:
        return await self.orders_api.get(broker_order_id, segment)

    async def get_order_by_reference(
        self, reference_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        return await self.orders_api.get_by_reference(reference_id, segment)

    async def list_orders(self, segment: Optional[Segment] = None) -> Sequence[BrokerOrder]:
        return await self.orders_api.list(segment)

    async def list_trades(
        self, broker_order_id: str, segment: Segment
    ) -> Sequence[BrokerTrade]:
        return await self.orders_api.trades(broker_order_id, segment)

    # --- Portfolio --------------------------------------------------------

    async def get_positions(self) -> Sequence[BrokerPosition]:
        return await self.portfolio_api.positions()

    async def get_holdings(self) -> Sequence[BrokerHolding]:
        return await self.portfolio_api.holdings()

    async def get_margin(self) -> MarginInfo:
        return await self.margin_api.available()


def _build(settings: Settings) -> BrokerProvider:
    return GrowwBrokerProvider(settings)


def register() -> None:
    from app.brokers.factory import register_broker_provider

    register_broker_provider(BrokerProviderName.GROWW, _build)


register()
