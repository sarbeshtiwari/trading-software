"""Groww margin and funds — GRW-015.

Available margin is a hard input to the risk engine (RISK-011) and to the sizer
(SIZE-004), so it is fetched from the broker rather than tracked locally: local
bookkeeping drifts, and the drift always shows up as an order the account cannot
actually support.

Where the broker exposes a required-margin calculation for a prospective order,
it is used. Where it does not, callers fall back to a local estimate that is
explicitly tagged ``ESTIMATED`` and carries a safety multiplier (FUT-004) — an
underestimate is the dangerous direction.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.mapping import parse_margin
from app.brokers.groww.ratelimit import RateCategory
from app.brokers.models import MarginInfo, OrderRequest
from app.core.errors import InvalidResponseError
from app.core.logging import get_logger

logger = get_logger("brokers.groww.margin")

__all__ = ["GrowwMarginApi"]


class GrowwMarginApi:
    def __init__(self, client: GrowwClient) -> None:
        self._client = client

    async def available(self) -> MarginInfo:
        result = await self._client.get(
            Endpoints.MARGIN.resolve(), category=RateCategory.NON_TRADING
        )
        payload = result if isinstance(result, dict) else {}
        margin = parse_margin(payload)
        logger.info(
            "Fetched broker margin",
            extra={
                "available_margin": str(margin.available_margin),
                "used_margin": str(margin.used_margin) if margin.used_margin else None,
                "source": margin.source.value,
            },
        )
        return margin

    async def required_for(
        self, requests: Sequence[OrderRequest]
    ) -> Optional[dict[str, Any]]:
        """Ask the broker what an order (or basket) would require.

        Returns ``None`` when the broker does not answer, so the caller can fall
        back to a labelled local estimate rather than assuming zero.
        """
        if not requests:
            return None

        body = {
            "segment": requests[0].segment.value,
            "orders": [
                {
                    "trading_symbol": request.trading_symbol,
                    "quantity": request.quantity,
                    "exchange": request.exchange.value,
                    "segment": request.segment.value,
                    "product": request.product.value,
                    "order_type": request.order_type.value,
                    "transaction_type": request.transaction_type.value,
                    **({"price": float(request.price)} if request.price is not None else {}),
                }
                for request in requests
            ],
        }

        try:
            result = await self._client.post(
                Endpoints.MARGIN_REQUIRED.resolve(),
                category=RateCategory.NON_TRADING,
                json=body,
            )
        except (InvalidResponseError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "Broker margin requirement unavailable; caller must use a labelled estimate",
                extra={"error": str(exc)},
            )
            return None

        return result if isinstance(result, dict) else None
