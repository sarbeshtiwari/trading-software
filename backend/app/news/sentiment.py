"""SENT-001: numeric, attributable news aggregation; source text is never executed.

This consumes verified, timestamped interpretation evidence, not raw headlines.
Weights are credibility * confidence / (1 + age / half_life). The evidence and
weights are returned for audit; absence of usable evidence is UNAVAILABLE.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.enums import VerificationStatus
from app.fno.chain.model import aware


class NewsEvidence(EvidenceModel):
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_ids: tuple[str, ...]
    best_tier: int = Field(ge=1, le=4, strict=True)
    verification: VerificationStatus | Literal["SOURCE_POLICY_ADMITTED"]
    published_at: AwareDatetime
    available_at: AwareDatetime
    score: Decimal = Field(ge=-1, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    confidence_basis: Literal["LEGACY_UNSPECIFIED", "MODEL_SELF_REPORT_UNCALIBRATED"] = (
        "LEGACY_UNSPECIFIED"
    )
    source_weight: Decimal = Field(default=Decimal(1), ge=0, le=1)

    @model_validator(mode="after")
    def evidence_identity(self):
        if not self.source_ids or any(not source.strip() for source in self.source_ids):
            raise ValueError("source identities required")
        if self.published_at > self.available_at:
            raise ValueError("news cannot be available before publication")
        return self


class NewsSentimentPolicy(EvidenceModel):
    tier_weights: tuple[Decimal, Decimal, Decimal, Decimal]
    half_life_seconds: int = Field(gt=0, strict=True)
    max_age_seconds: int = Field(gt=0, strict=True)
    minimum_confidence: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_weights(self):
        if any(weight < 0 for weight in self.tier_weights) or not any(self.tier_weights):
            raise ValueError("non-negative, nonzero credibility weights required")
        return self


class NewsContribution(EvidenceModel):
    evidence: NewsEvidence
    weight: Decimal


class NewsSentiment(EvidenceModel):
    formula: Literal[
        "CREDIBILITY_CONFIDENCE_RECENCY_V1", "CREDIBILITY_SOURCE_CONFIDENCE_RECENCY_V2"
    ] = "CREDIBILITY_CONFIDENCE_RECENCY_V1"
    score: Decimal | None
    contributions: tuple[NewsContribution, ...]
    excluded: dict[str, str]
    policy: NewsSentimentPolicy
    standalone_trigger_allowed: Literal[False] = False


def aggregate_news(
    items: list[NewsEvidence],
    *,
    symbol: str,
    as_of: datetime,
    policy: NewsSentimentPolicy,
) -> NewsSentiment:
    aware(as_of)
    if len({item.id for item in items}) != len(items):
        raise ValueError("duplicate news evidence")
    included, excluded = [], {}
    for item in sorted(items, key=lambda item: item.id):
        reason = _exclusion(item, symbol, as_of, policy)
        if reason:
            excluded[item.id] = reason
            continue
        age = as_of - item.published_at
        seconds = Decimal(age.days * 86400 + age.seconds) + Decimal(age.microseconds) / 1000000
        weight = (
            policy.tier_weights[item.best_tier - 1]
            * item.source_weight
            * item.confidence
            / (1 + seconds / policy.half_life_seconds)
        )
        if weight:
            included.append(NewsContribution(evidence=item, weight=weight))
        else:
            excluded[item.id] = "ZERO_WEIGHT"
    total = sum((item.weight for item in included), Decimal(0))
    score = (
        sum((item.weight * item.evidence.score for item in included), Decimal(0)) / total
        if total
        else None
    )
    return NewsSentiment(
        score=score,
        contributions=tuple(included),
        excluded=excluded,
        policy=policy,
        formula="CREDIBILITY_SOURCE_CONFIDENCE_RECENCY_V2"
        if any(
            item.source_weight != 1 or item.verification == "SOURCE_POLICY_ADMITTED"
            for item in items
        )
        else "CREDIBILITY_CONFIDENCE_RECENCY_V1",
    )


def _exclusion(item, symbol, as_of, policy):
    if item.symbol != symbol:
        return "OTHER_INSTRUMENT"
    if item.available_at > as_of:
        return "FUTURE_KNOWLEDGE"
    if item.verification not in (VerificationStatus.VERIFIED, "SOURCE_POLICY_ADMITTED"):
        return "NOT_VERIFIED"
    if item.best_tier != 1 and len(set(item.source_ids)) < 2:
        return "INSUFFICIENT_CORROBORATION"
    if (as_of - item.published_at).total_seconds() > policy.max_age_seconds:
        return "STALE"
    return "LOW_CONFIDENCE" if item.confidence < policy.minimum_confidence else None
