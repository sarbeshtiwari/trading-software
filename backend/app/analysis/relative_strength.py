"""EQ-003: relative wealth change versus a benchmark, with aligned session endpoints."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.analysis.equity import EquitySnapshot, EvidenceModel, aligned_history


class StrengthRanking(EvidenceModel):
    ranked: tuple[tuple[str, Decimal], ...]
    unavailable: dict[str, str]
    lookback: int


def rank_strength(
    members: list[EquitySnapshot],
    benchmark: EquitySnapshot,
    *,
    lookback: int,
    as_of: datetime,
    max_history_age: timedelta,
) -> StrengthRanking:
    if len({member.key for member in members}) != len(members):
        raise ValueError("duplicate ranking member")
    ranked, unavailable = [], {}
    for member in members:
        try:
            stock, index = aligned_history(
                member, benchmark, as_of=as_of, lookback=lookback, max_age=max_history_age
            )
            score = (stock[-1].close / stock[0].close) / (index[-1].close / index[0].close) - 1
            ranked.append((member.key, score))
        except ValueError as exc:
            unavailable[member.key] = str(exc)
    return StrengthRanking(
        ranked=tuple(sorted(ranked, key=lambda item: (-item[1], item[0]))),
        unavailable=unavailable,
        lookback=lookback,
    )
