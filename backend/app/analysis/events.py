"""EQ-007: typed event blackout context; absent coverage never means clear."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.analysis.regime.events import EventCalendar


class EventBlackout(EvidenceModel):
    symbol: str
    as_of: AwareDatetime
    status: Literal["BLACKOUT", "CLEAR", "UNAVAILABLE"]
    event_ids: tuple[str, ...]
    restricted_strategies: tuple[str, ...]


def event_blackout(
    symbol: str,
    *,
    as_of: datetime,
    calendar: EventCalendar | None,
    restricted_strategies: tuple[str, ...],
) -> EventBlackout:
    active = calendar.active(symbol, as_of) if calendar else None
    status = "UNAVAILABLE" if active is None else "BLACKOUT" if active else "CLEAR"
    return EventBlackout(
        as_of=as_of,
        symbol=symbol,
        status=status,
        event_ids=active or (),
        restricted_strategies=restricted_strategies,
    )
