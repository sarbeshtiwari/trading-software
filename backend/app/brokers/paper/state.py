"""Paper-broker persistence — PAPER-005.

A paper run that forgets its positions on restart is not a rehearsal for live
trading; it is a rehearsal for losing track of positions. State is therefore
written to the database on every mutation and reloaded at startup, exactly as the
real broker's state would be re-fetched.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from app.core.errors import SafetyError
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.execution import PaperStateRevision
from app.db.models.paper import PAPER_STATE_ID, PaperBrokerState
from app.modes import TradingMode

logger = get_logger("brokers.paper.state")

__all__ = ["InMemoryPaperStateStore", "PaperStateStore"]


class PaperStateStore:
    """Database-backed snapshot store."""

    def __init__(self, mode: TradingMode = TradingMode.PAPER) -> None:
        self._mode = mode
        self._version = 0

    async def load(self) -> dict[str, Any] | None:
        async with db_session.session_scope() as session:
            revision = await session.get(PaperStateRevision, PAPER_STATE_ID)
            self._version = revision.version if revision else 0
            row = await session.get(PaperBrokerState, PAPER_STATE_ID)
            if row is None:
                return None
            return {
                "account": row.account,
                "orders": row.orders,
                "trades": row.trades,
                "reference_index": row.reference_index,
                "fill_config": row.fill_config,
            }

    async def save(self, snapshot: dict[str, Any]) -> None:
        async with db_session.session_scope() as session:
            if self._version == 0:
                session.add(PaperStateRevision(id=PAPER_STATE_ID, version=1))
                await session.flush()
            else:
                changed = await session.execute(
                    sa.update(PaperStateRevision)
                    .where(
                        PaperStateRevision.id == PAPER_STATE_ID,
                        PaperStateRevision.version == self._version,
                    )
                    .values(version=self._version + 1)
                )
                if changed.rowcount != 1:
                    raise SafetyError("Concurrent paper state mutation; reload and reconcile")
            row = await session.get(PaperBrokerState, PAPER_STATE_ID)
            if row is None:
                row = PaperBrokerState(id=PAPER_STATE_ID, mode=self._mode)
                session.add(row)
            row.account = snapshot.get("account", {})
            row.orders = snapshot.get("orders", {})
            row.trades = snapshot.get("trades", [])
            row.reference_index = snapshot.get("reference_index", {})
            row.fill_config = snapshot.get("fill_config")
        self._version += 1

    async def clear(self) -> None:
        async with db_session.session_scope() as session:
            await session.execute(
                sa.delete(PaperBrokerState).where(PaperBrokerState.id == PAPER_STATE_ID)
            )
            await session.execute(
                sa.delete(PaperStateRevision).where(PaperStateRevision.id == PAPER_STATE_ID)
            )
        self._version = 0


class InMemoryPaperStateStore:
    """Non-persistent store for tests and throwaway runs.

    Kept explicit rather than making persistence optional by accident: a paper
    broker that quietly stops persisting is a paper broker whose results cannot
    be trusted as evidence for LIVE approval.
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any] | None:
        return self._snapshot

    async def save(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def clear(self) -> None:
        self._snapshot = None
