"""SENT-002: transparent market composite from sourced observations, never a signal."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.analysis.regime.inputs import Observation
from app.fno.chain.model import aware


class MarketSentimentPolicy(EvidenceModel):
    return_scale: Decimal = Field(gt=0)
    volatility_scale: Decimal = Field(gt=0)
    neutral_volatility: Decimal = Field(ge=0)
    max_age_seconds: int = Field(gt=0, strict=True)


class MarketSentiment(EvidenceModel):
    formula: Literal["EQUAL_COMPONENTS_V1"] = "EQUAL_COMPONENTS_V1"
    score: Decimal | None
    components: dict[str, Decimal]
    evidence: dict[str, Observation | None]
    unavailable: tuple[str, ...]
    policy: MarketSentimentPolicy
    standalone_trigger_allowed: Literal[False] = False


def market_sentiment(
    *,
    index_return: Observation | None,
    breadth: Observation | None,
    volatility: Observation | None,
    as_of: datetime,
    policy: MarketSentimentPolicy,
) -> MarketSentiment:
    aware(as_of)
    evidence = {"index_return": index_return, "breadth": breadth, "volatility": volatility}
    missing = tuple(
        name
        for name, item in evidence.items()
        if item is None
        or item.available_at > as_of
        or as_of - item.observed_at > timedelta(seconds=policy.max_age_seconds)
    )
    components = {}
    if not missing:
        if index_return.value < -1 or not 0 <= breadth.value <= 1 or volatility.value < 0:
            raise ValueError("invalid market sentiment observation")
        components = {
            "index_return": _clip(index_return.value / policy.return_scale),
            "breadth": breadth.value * 2 - 1,
            "volatility": _clip(
                (policy.neutral_volatility - volatility.value) / policy.volatility_scale
            ),
        }
    return MarketSentiment(
        score=sum(components.values()) / 3 if components else None,
        components=components,
        evidence=evidence,
        unavailable=missing,
        policy=policy,
    )


def _clip(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(-1), value))
