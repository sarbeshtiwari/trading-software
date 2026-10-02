"""Subscription management — MD-004.

Several components want live prices for overlapping sets of instruments: open
positions, working orders, the strategies being evaluated, the dashboard
watchlist. Each registers *interest*; this module turns the union of those
interests into feed subscriptions, within the 1000-instrument budget, and
unsubscribes only when the last interested party lets go.

Priority is derived from who wants it, so when the budget is full the thing that
gets dropped is a watchlist entry rather than an instrument the account has money
in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from app.brokers.groww.feed import FeedPriority, GrowwFeedClient, Subscription
from app.core.logging import get_logger
from app.marketdata.models import InstrumentRef

logger = get_logger("marketdata.subscriptions")

__all__ = ["Interest", "SubscriptionManager", "PRIORITY_BY_SOURCE"]

#: Who is asking -> how much it matters.
PRIORITY_BY_SOURCE: dict[str, FeedPriority] = {
    "position": FeedPriority.POSITION,
    "order": FeedPriority.ORDER,
    "strategy": FeedPriority.STRATEGY,
    "watchlist": FeedPriority.WATCH,
    "index": FeedPriority.WATCH,
}


@dataclass(frozen=True)
class Interest:
    instrument: InstrumentRef
    source: str
    kind: str = "ltp"

    @property
    def priority(self) -> FeedPriority:
        return PRIORITY_BY_SOURCE.get(self.source, FeedPriority.WATCH)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.instrument.key}"


@dataclass
class SubscriptionManager:
    feed: Optional[GrowwFeedClient] = None
    _interests: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set), init=False)
    _instruments: dict[str, Interest] = field(default_factory=dict, init=False)

    def attach(self, feed: GrowwFeedClient) -> None:
        self.feed = feed

    # --- Interest registration --------------------------------------------

    async def register(self, interests: Iterable[Interest]) -> list[Subscription]:
        """Record interest and subscribe to anything newly wanted."""
        new: list[Subscription] = []

        for interest in interests:
            holders = self._interests[interest.key]
            first = not holders
            holders.add(interest.source)

            existing = self._instruments.get(interest.key)
            if existing is None or interest.priority > existing.priority:
                self._instruments[interest.key] = interest

            if first or (existing is not None and interest.priority > existing.priority):
                new.append(
                    Subscription(
                        instrument=interest.instrument,
                        priority=self._instruments[interest.key].priority,
                        kind=interest.kind,
                    )
                )

        if new and self.feed is not None:
            accepted = await self.feed.subscribe(new)
            if len(accepted) != len(new):
                logger.warning(
                    "Some subscriptions were refused by the budget",
                    extra={"requested": len(new), "accepted": len(accepted)},
                )
            return accepted
        return new

    async def release(self, interests: Iterable[Interest]) -> list[Subscription]:
        """Drop interest, unsubscribing only when nobody is left wanting it."""
        dropped: list[Subscription] = []

        for interest in interests:
            holders = self._interests.get(interest.key)
            if not holders:
                continue
            holders.discard(interest.source)
            if holders:
                # Someone else still wants it; recompute the effective priority.
                remaining = max(
                    (PRIORITY_BY_SOURCE.get(source, FeedPriority.WATCH) for source in holders),
                    default=FeedPriority.WATCH,
                )
                current = self._instruments.get(interest.key)
                if current is not None and remaining != current.priority:
                    self._instruments[interest.key] = Interest(
                        instrument=current.instrument,
                        source=current.source,
                        kind=current.kind,
                    )
                continue

            self._interests.pop(interest.key, None)
            held = self._instruments.pop(interest.key, None)
            if held is not None:
                dropped.append(
                    Subscription(
                        instrument=held.instrument, priority=held.priority, kind=held.kind
                    )
                )

        if dropped and self.feed is not None:
            await self.feed.unsubscribe(dropped)
        return dropped

    async def release_source(self, source: str) -> list[Subscription]:
        """Drop everything one source was interested in (e.g. a disabled strategy)."""
        interests = [
            Interest(instrument=held.instrument, source=source, kind=held.kind)
            for key, held in list(self._instruments.items())
            if source in self._interests.get(key, set())
        ]
        return await self.release(interests)

    # --- Introspection -----------------------------------------------------

    @property
    def subscribed(self) -> list[InstrumentRef]:
        return [interest.instrument for interest in self._instruments.values()]

    def holders(self, interest_key: str) -> set[str]:
        return set(self._interests.get(interest_key, ()))

    def __len__(self) -> int:
        return len(self._instruments)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "instrument": interest.instrument.key,
                "kind": interest.kind,
                "priority": int(interest.priority),
                "holders": sorted(self._interests.get(key, ())),
            }
            for key, interest in self._instruments.items()
        ]
