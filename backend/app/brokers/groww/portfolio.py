"""Groww positions and holdings — GRW-013, GRW-014.

Positions are the authority during reconciliation (PORT-008), so parsing failures
here must be loud: a position the adapter silently drops becomes a position the
risk engine does not know it is carrying.

Holdings (delivery, CNC) are kept separate from intraday positions throughout the
system — the same instrument can legitimately appear in both.
"""

from __future__ import annotations

from typing import Optional

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.mapping import parse_holding, parse_list, parse_position
from app.brokers.groww.ratelimit import RateCategory
from app.brokers.models import BrokerHolding, BrokerPosition
from app.core.enums import Segment
from app.core.logging import get_logger

logger = get_logger("brokers.groww.portfolio")

__all__ = ["GrowwPortfolioApi"]


class GrowwPortfolioApi:
    def __init__(self, client: GrowwClient) -> None:
        self._client = client

    async def positions(self, segment: Optional[Segment] = None) -> list[BrokerPosition]:
        params = {"segment": segment.value} if segment is not None else None
        result = await self._client.get(
            Endpoints.POSITIONS.resolve(),
            category=RateCategory.NON_TRADING,
            params=params,
        )
        rows = parse_list(result, "positions", "position_list")
        positions = [parse_position(row) for row in rows]

        logger.info(
            "Fetched broker positions",
            extra={
                "count": len(positions),
                "open": sum(1 for p in positions if p.net_quantity != 0),
            },
        )
        return positions

    async def holdings(self) -> list[BrokerHolding]:
        result = await self._client.get(
            Endpoints.HOLDINGS.resolve(), category=RateCategory.NON_TRADING
        )
        rows = parse_list(result, "holdings", "holding_list")
        holdings = [parse_holding(row) for row in rows]

        unmapped = [h.trading_symbol for h in holdings if not h.isin]
        if unmapped:
            # Flagged rather than dropped: a holding without an ISIN is still a
            # holding, and pretending it does not exist understates exposure.
            logger.warning(
                "Holdings returned without an ISIN",
                extra={"symbols": unmapped[:20], "count": len(unmapped)},
            )
        return holdings
