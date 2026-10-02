"""BrokerProvider interface — ARCH-004.

Exactly one abstraction stands between the trading engine and where orders go.
``GrowwBrokerProvider`` and ``PaperBrokerProvider`` both implement it in full, so
the same strategy, sizing and risk code runs unchanged against either (ARCH-007).

Every method is abstract on purpose: a provider that silently inherits a no-op
``place_order`` would be a provider that silently loses trades. The parity test
asserts both implementations override every abstract method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

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
from app.core.data_origin import ExecutionRealism
from app.core.enums import Segment

__all__ = ["BrokerProvider"]


class BrokerProvider(ABC):
    """Everything the system may ask of a broker."""

    #: Short name used in logs, config and audit records.
    name: str = "abstract"

    @property
    @abstractmethod
    def execution_realism(self) -> ExecutionRealism:
        """Whether fills from this provider are real or simulated."""

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Declared feature set, used to reject unsupported requests locally."""

    # --- Connection -------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and prepare for use. Idempotent."""

    @abstractmethod
    async def close(self) -> None:
        """Release connections."""

    @abstractmethod
    async def ping(self) -> bool:
        """Cheap read-only call proving credentials and connectivity work."""

    @abstractmethod
    async def get_profile(self) -> BrokerProfile:
        """Identity of the authenticated account."""

    # --- Orders -----------------------------------------------------------

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderAck:
        """Submit an order.

        Implementations must be idempotent with respect to
        ``request.reference_id``: a duplicate submission must resolve to the
        existing order rather than creating a second one (EXEC-004).
        """

    @abstractmethod
    async def modify_order(self, request: ModifyRequest) -> OrderAck:
        """Modify a pending or open order."""

    @abstractmethod
    async def cancel_order(self, broker_order_id: str, segment: Segment) -> OrderAck:
        """Cancel an order. Cancelling an already-cancelled order must succeed."""

    @abstractmethod
    async def get_order(self, broker_order_id: str, segment: Segment) -> BrokerOrder:
        """Fetch one order by the broker's id."""

    @abstractmethod
    async def get_order_by_reference(
        self, reference_id: str, segment: Segment
    ) -> Optional[BrokerOrder]:
        """Fetch one order by our reference id.

        This is the call that makes a timed-out submission safe: before any retry
        the executor asks whether the broker already has the order (EXEC-005).
        Returns ``None`` when the broker has no such order.
        """

    @abstractmethod
    async def list_orders(self, segment: Optional[Segment] = None) -> Sequence[BrokerOrder]:
        """All of today's orders, across pages."""

    @abstractmethod
    async def list_trades(
        self, broker_order_id: str, segment: Segment
    ) -> Sequence[BrokerTrade]:
        """Fills for one order, across pages."""

    # --- Portfolio --------------------------------------------------------

    @abstractmethod
    async def get_positions(self) -> Sequence[BrokerPosition]:
        """Current positions. The authority during reconciliation (PORT-008)."""

    @abstractmethod
    async def get_holdings(self) -> Sequence[BrokerHolding]:
        """Delivery holdings."""

    @abstractmethod
    async def get_margin(self) -> MarginInfo:
        """Available and used margin."""
