"""EQ-006: session gap candidates; official open is the explicit pre-open fallback."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.analysis.equity import EquitySnapshot, EvidenceModel
from app.core.clock import IST
from app.fno.chain.model import aware


class GapCandidate(EvidenceModel):
    symbol: str
    gap_percent: Decimal
    previous_close: Decimal
    price: Decimal
    method: Literal["PREOPEN", "OFFICIAL_OPEN"]


def gap_analysis(
    snapshot: EquitySnapshot, *, as_of: datetime, max_age: timedelta, previous_session: date
) -> GapCandidate | None:
    aware(as_of)
    if snapshot.known_at > as_of or max_age < timedelta(0):
        raise ValueError("future snapshot or invalid freshness policy")
    today = as_of.astimezone(IST).date()
    previous = [bar for bar in snapshot.daily if bar.closed_at.astimezone(IST).date() < today]
    if not previous or previous[-1].closed_at.astimezone(IST).date() != previous_session:
        return None
    for observation, method in (
        (snapshot.preopen, "PREOPEN"),
        (snapshot.official_open, "OFFICIAL_OPEN"),
    ):
        if (
            observation is not None
            and observation.available_at <= as_of
            and observation.observed_at.astimezone(IST).date() == today
            and as_of - observation.observed_at <= max_age
        ):
            return GapCandidate(
                symbol=snapshot.key,
                gap_percent=(observation.value / previous[-1].close - 1) * 100,
                previous_close=previous[-1].close,
                price=observation.value,
                method=method,
            )
    return None


def rank_gaps(candidates: list[GapCandidate]) -> tuple[GapCandidate, ...]:
    return tuple(sorted(candidates, key=lambda item: (-abs(item.gap_percent), item.symbol)))
