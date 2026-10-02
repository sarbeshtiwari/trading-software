"""Owner-enabled advisory scoring of admitted quotations using the shared aggregator."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, model_validator
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.modes import TradingMode
from app.news.entities import aware, history
from app.news.interpret import read_interpretation
from app.news.research import admitted_for_instrument
from app.news.sentiment import NewsEvidence, NewsSentiment, NewsSentimentPolicy, aggregate_news

POLICY_CHAIN = "news-advisory-sentiment-policy"


class AdvisorySentimentPolicy(EvidenceModel):
    version: Literal[1] = 1
    enabled: bool
    accept_uncalibrated_model_scores: bool
    aggregation: NewsSentimentPolicy
    source_weight_rule: Literal["MIN_SUPPORTING_SOURCE_WEIGHT_V1"] = (
        "MIN_SUPPORTING_SOURCE_WEIGHT_V1"
    )
    mapping: Literal["POSITIVE_1_NEGATIVE_MINUS1_NEUTRAL_0_V1"] = (
        "POSITIVE_1_NEGATIVE_MINUS1_NEUTRAL_0_V1"
    )

    @model_validator(mode="after")
    def explicit_consent(self):
        if self.enabled and not self.accept_uncalibrated_model_scores:
            raise ValueError("Explicit acceptance of uncalibrated advisory scores is required")
        return self


class SentimentPolicyView(EvidenceModel):
    event_id: str | None
    known_at: AwareDatetime | None
    policy: AdvisorySentimentPolicy | None


class AdvisorySentiment(EvidenceModel):
    status: Literal["AVAILABLE", "UNAVAILABLE", "DEGRADED"]
    code: str
    instrument_id: str
    as_of: AwareDatetime
    data_origin: DataOrigin
    policy: SentimentPolicyView
    result: NewsSentiment | None
    admission_ids: tuple[str, ...] = ()
    confidence_basis: Literal["MODEL_SELF_REPORT_UNCALIBRATED"] = "MODEL_SELF_REPORT_UNCALIBRATED"
    aggregation_key: Literal["CANONICAL_INSTRUMENT_ID"] = "CANONICAL_INSTRUMENT_ID"
    standalone_trigger_allowed: Literal[False] = False


async def policy_at(session, cutoff):
    records = await history(session, POLICY_CHAIN, cutoff)
    if not records:
        return SentimentPolicyView(event_id=None, known_at=None, policy=None)
    event = records[-1]
    if event.event_type != "NEWS_SENTIMENT_POLICY":
        raise ValueError("Sentiment policy integrity unavailable")
    return SentimentPolicyView(
        event_id=event.id,
        known_at=aware(event.occurred_at),
        policy=AdvisorySentimentPolicy.model_validate(event.result["policy"]),
    )


async def configure_policy(session, policy, *, expected_event_id, actor, reason):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise ValueError("Sentiment controls are PAPER-only")
    policy = AdvisorySentimentPolicy.model_validate(policy.model_dump())
    now = get_clock().utcnow()
    records = await history(session, POLICY_CHAIN)
    if (
        (records[-1].id if records else None) != expected_event_id
        or (records and aware(records[-1].occurred_at) > now)
        or len(reason.strip()) < 10
    ):
        raise ValueError("Sentiment policy changed or reason unavailable")
    event = await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=POLICY_CHAIN,
            event_type="NEWS_SENTIMENT_POLICY",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": {"policy": policy.model_dump(mode="json"), "reason": reason.strip()}},
        expected_count=len(records),
    )
    if aware(event.occurred_at) < now:
        raise ValueError("Sentiment policy clock regression")
    return SentimentPolicyView(event_id=event.id, known_at=aware(event.occurred_at), policy=policy)


def claim_key(claim):
    return tuple(claim[field] for field in ("article_id", "field", "start", "end", "quote"))


async def evidence_for(session, snapshot, instrument_id, policy, cutoff):
    review = await read_interpretation(session, snapshot["article_id"], as_of=cutoff)
    if review is None or review.event_id != snapshot["interpretation_event_id"]:
        return None, "INTERPRETATION_UNAVAILABLE"
    parsed = review.result.result.interpretation
    if parsed.instrument_ids != (instrument_id,):
        return None, "INTERPRETATION_INSTRUMENT_SCOPE"
    admitted_claims = {
        claim_key(support["claim"]) for group in snapshot["claims"] for support in group
    }
    if any(claim_key(claim.model_dump()) not in admitted_claims for claim in parsed.claims):
        return None, "INTERPRETATION_CLAIM_SCOPE"
    score = {"POSITIVE": Decimal(1), "NEGATIVE": Decimal(-1), "NEUTRAL": Decimal(0)}.get(
        parsed.direction
    )
    if score is None:
        return None, "DIRECTION_UNAVAILABLE"
    evidence = NewsEvidence(
        id=snapshot["source_id"],
        symbol=instrument_id,
        url=snapshot["sources"][0]["url"],
        source_ids=tuple(sorted({source["publisher_id"] for source in snapshot["sources"]})),
        best_tier=snapshot["best_tier"],
        verification="SOURCE_POLICY_ADMITTED",
        published_at=datetime.fromisoformat(snapshot["published_at"]),
        available_at=max(
            datetime.fromisoformat(snapshot["known_at"]), review.known_at, policy.known_at
        ),
        score=score,
        confidence=parsed.confidence,
        confidence_basis="MODEL_SELF_REPORT_UNCALIBRATED",
        source_weight=min(Decimal(source["weight"]) for source in snapshot["sources"]),
    )
    return evidence, None


async def sentiment_at(instrument_id, *, as_of, origin, max_age_seconds):
    if as_of.utcoffset() is None:
        raise ValueError("Sentiment cutoff must be timezone-aware")
    empty_policy = SentimentPolicyView(event_id=None, known_at=None, policy=None)
    base = {
        "instrument_id": instrument_id,
        "as_of": as_of,
        "data_origin": origin,
        "policy": empty_policy,
        "result": None,
    }
    if as_of > get_clock().utcnow() or max_age_seconds <= 0:
        return AdvisorySentiment(status="UNAVAILABLE", code="SENTIMENT_TIME_UNAVAILABLE", **base)
    cutoff = as_of.astimezone(UTC)
    try:
        async with db_session.session_scope() as session:
            policy = await policy_at(session, cutoff)
            base["policy"] = policy
            if policy.policy is None or not policy.policy.enabled:
                return AdvisorySentiment(
                    status="UNAVAILABLE", code="NEWS_SENTIMENT_POLICY_UNAVAILABLE", **base
                )
            effective = policy.policy.aggregation.model_copy(
                update={
                    "max_age_seconds": min(
                        max_age_seconds, policy.policy.aggregation.max_age_seconds
                    )
                }
            )
            snapshots = await admitted_for_instrument(
                session,
                instrument_id,
                as_of=cutoff,
                max_age=timedelta(seconds=effective.max_age_seconds),
                origin=origin,
            )
            evidence, excluded = [], {}
            for snapshot in snapshots:
                item, reason = await evidence_for(session, snapshot, instrument_id, policy, cutoff)
                if item is None:
                    excluded[snapshot["source_id"]] = reason
                else:
                    evidence.append(item)
            aggregate = aggregate_news(
                evidence, symbol=instrument_id, as_of=cutoff, policy=effective
            )
            aggregate = aggregate.model_copy(
                update={"excluded": {**excluded, **aggregate.excluded}}
            )
            base["result"] = aggregate
            return AdvisorySentiment(
                status="AVAILABLE" if aggregate.score is not None else "UNAVAILABLE",
                code="NEWS_SENTIMENT_AVAILABLE"
                if aggregate.score is not None
                else "NEWS_SENTIMENT_UNAVAILABLE",
                admission_ids=tuple(item.evidence.id for item in aggregate.contributions),
                **base,
            )
    except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError):
        return AdvisorySentiment(status="DEGRADED", code="NEWS_SENTIMENT_DEGRADED", **base)
