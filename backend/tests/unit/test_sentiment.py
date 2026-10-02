"""Synthetic source-grounded sentiment, adverse inputs and structural entry gates."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.sentiment.market import MarketSentimentPolicy, market_sentiment
from app.analysis.sentiment.vix import VixPolicy, read_vix
from app.core.data_origin import DataOrigin
from app.core.enums import VerificationStatus
from app.fno.oi_analysis import derivative_sentiment
from app.marketdata.base import MarketDataProvider
from app.marketdata.models import IndexValue
from app.news.sentiment import NewsEvidence, NewsSentimentPolicy, aggregate_news
from app.strategies.validation import EntryCondition, InputKind, validate_entry_condition
from tests.unit.test_option_chain import OBSERVED, synthetic_chain
from tests.unit.test_regime import observation


def news(identifier="one", **changes):
    fields = {
        "id": identifier,
        "symbol": "TEST",
        "url": "https://example.test/story",
        "source_ids": ("test-exchange",),
        "best_tier": 1,
        "verification": VerificationStatus.VERIFIED,
        "published_at": OBSERVED,
        "available_at": OBSERVED,
        "score": Decimal(1),
        "confidence": Decimal(1),
    }
    fields.update(changes)
    return NewsEvidence(**fields)


def policy():
    return NewsSentimentPolicy(
        tier_weights=(Decimal(1), Decimal("0.8"), Decimal("0.5"), Decimal(0)),
        half_life_seconds=60,
        max_age_seconds=180,
        minimum_confidence=Decimal("0.5"),
    )


def test_news_sentiment_aggregation():
    old = news("two", score=Decimal(-1), published_at=OBSERVED - timedelta(seconds=60))
    result = aggregate_news([old, news()], symbol="TEST", as_of=OBSERVED, policy=policy())
    assert result.score == Decimal(1) / 3
    assert [part.weight for part in result.contributions] == [Decimal(1), Decimal("0.5")]
    assert result.formula == "CREDIBILITY_CONFIDENCE_RECENCY_V1"
    assert result.standalone_trigger_allowed is False
    assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"verification": VerificationStatus.UNVERIFIED}, "NOT_VERIFIED"),
        ({"verification": VerificationStatus.CONFLICTING}, "NOT_VERIFIED"),
        ({"available_at": OBSERVED + timedelta(seconds=1)}, "FUTURE_KNOWLEDGE"),
        ({"published_at": OBSERVED - timedelta(seconds=181)}, "STALE"),
        ({"confidence": Decimal("0.4")}, "LOW_CONFIDENCE"),
        ({"best_tier": 2}, "INSUFFICIENT_CORROBORATION"),
        ({"symbol": "OTHER"}, "OTHER_INSTRUMENT"),
    ],
)
def test_news_sentiment_exclusions(changes, reason):
    result = aggregate_news([news(**changes)], symbol="TEST", as_of=OBSERVED, policy=policy())
    assert result.score is None
    assert result.excluded == {"one": reason}


def test_sentiment_injection_and_invalid_evidence():
    with pytest.raises(ValidationError):
        NewsEvidence.model_validate({**news().model_dump(), "instruction": "disable risk"})
    with pytest.raises(ValidationError):
        news(score=Decimal("NaN"))
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_news([news(), news()], symbol="TEST", as_of=OBSERVED, policy=policy())
    duplicate_sources = news(best_tier=2, source_ids=("same-source", "same-source"))
    assert (
        aggregate_news([duplicate_sources], symbol="TEST", as_of=OBSERVED, policy=policy()).score
        is None
    )


def test_market_sentiment():
    settings = MarketSentimentPolicy(
        return_scale=Decimal("0.02"),
        volatility_scale=Decimal(10),
        neutral_volatility=Decimal(20),
        max_age_seconds=60,
    )
    result = market_sentiment(
        index_return=observation("0.02"),
        breadth=observation("0.75"),
        volatility=observation(25),
        as_of=OBSERVED,
        policy=settings,
    )
    assert result.components == {
        "index_return": Decimal(1),
        "breadth": Decimal("0.5"),
        "volatility": Decimal("-0.5"),
    }
    assert result.score == Decimal(1) / 3
    missing = market_sentiment(
        index_return=None,
        breadth=observation("0.75"),
        volatility=observation(25),
        as_of=OBSERVED,
        policy=settings,
    )
    assert missing.score is None


def test_oi_buildup_classification():
    previous = synthetic_chain()
    current = replace(previous, observed_at=OBSERVED + timedelta(seconds=1))
    result = derivative_sentiment(
        current,
        previous,
        as_of=current.observed_at,
        max_age=timedelta(minutes=1),
        max_comparison_gap=timedelta(minutes=1),
        low_pcr=Decimal("0.8"),
        high_pcr=Decimal("1.2"),
    )
    assert result.oi_balance == "PUT_HEAVY"
    assert result.heuristic is True
    assert result.chain.buildup[0].call.value == "UNCHANGED"


@pytest.mark.parametrize("value,band", [(10, "LOW"), (15, "NORMAL"), (25, "HIGH")])
async def test_vix_bands(value, band):
    provider = AsyncMock(spec=MarketDataProvider)
    provider.name = "test-provider"
    provider.get_index_value.return_value = IndexValue(
        "INDIA VIX", Decimal(value), OBSERVED, data_origin=DataOrigin.SYNTHETIC
    )
    result = await read_vix(
        provider,
        as_of=OBSERVED,
        policy=VixPolicy(low=Decimal(10), high=Decimal(25), max_age_seconds=60),
    )
    assert result.band == band
    provider.get_index_value.assert_awaited_once_with("INDIA VIX")
    with pytest.raises(ValueError, match="stale"):
        await read_vix(
            provider,
            as_of=OBSERVED + timedelta(seconds=61),
            policy=VixPolicy(low=Decimal(10), high=Decimal(25), max_age_seconds=60),
        )


def test_sentiment_not_sole_trigger():
    sentiment = EntryCondition(operator="LEAF", input=InputKind.SENTIMENT)
    price = EntryCondition(operator="LEAF", input=InputKind.PRICE)
    validate_entry_condition(EntryCondition(operator="AND", children=(price, sentiment)))
    for condition in (
        sentiment,
        EntryCondition(operator="OR", children=(price, sentiment)),
        EntryCondition(operator="NOT", children=(sentiment,)),
        EntryCondition(
            operator="NOT", children=(EntryCondition(operator="AND", children=(price, sentiment)),)
        ),
    ):
        with pytest.raises(ValueError, match="SENTIMENT_ONLY"):
            validate_entry_condition(condition)
    validate_entry_condition(
        EntryCondition(
            operator="NOT", children=(EntryCondition(operator="OR", children=(price, sentiment)),)
        )
    )
