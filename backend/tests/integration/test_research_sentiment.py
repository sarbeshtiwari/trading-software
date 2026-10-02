"""Actual admitted evidence and owner policy reach the shared math and decision gates."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.agents.pipeline import DecisionPipeline
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.news.research_sentiment import sentiment_at
from app.strategies.registry import StrategyRegistry
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_availability import news_context
from tests.integration.test_news_research_admission import interpret, observe
from tests.integration.test_proposal import validator
from tests.integration.test_strategies import evaluate
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy, available

__all__ = ["credentials", "journal_client"]


def policy_request(*, enabled=True, expected=None):
    return {
        "expected_event_id": expected,
        "reason": "Owner selects isolated advisory scoring policy",
        "policy": {
            "enabled": enabled,
            "accept_uncalibrated_model_scores": True,
            "aggregation": {
                "tier_weights": ["1", "0.8", "0.5", "0"],
                "half_life_seconds": 60,
                "max_age_seconds": 180,
                "minimum_confidence": "0.5",
            },
        },
    }


async def seed(client, monkeypatch, name, direction, *, weight="1", confidence="1"):
    quote = f"NSE:TEST reported a synthetic {name} corporate announcement."
    article_id, _version = await observe(
        client,
        name,
        primary=True,
        quote=quote,
        title=f"Synthetic {name} announcement",
        weight=weight,
    )
    request = await interpret(
        client, article_id, monkeypatch, quote=quote, direction=direction, confidence=confidence
    )
    response = await client.post(f"/api/v1/news/articles/{article_id}/research", json=request)
    assert response.status_code == 200, response.text
    return response.json()["source_id"]


async def score(client, **params):
    return await client.get(
        "/api/v1/news/sentiment/ins-test", params={"origin": "SYNTHETIC", **params}
    )


async def test_advisory_policy_weighting_restart_and_historical_disable(
    journal_client, fake_clock, monkeypatch
):
    await news_context()
    await seed(journal_client, monkeypatch, "positive", "POSITIVE")
    await seed(journal_client, monkeypatch, "negative", "NEGATIVE", weight="0.5")
    assert (await score(journal_client)).json()["code"] == "NEWS_SENTIMENT_POLICY_UNAVAILABLE"
    configured = await journal_client.put("/api/v1/news/sentiment-policy", json=policy_request())
    assert configured.status_code == 200, configured.text
    result = await score(journal_client)
    assert result.status_code == 200, result.text
    view = result.json()
    assert view["status"] == "AVAILABLE"
    assert Decimal(view["result"]["score"]) == Decimal(1) / 3
    assert sorted(Decimal(item["weight"]) for item in view["result"]["contributions"]) == [
        Decimal("0.5"),
        Decimal(1),
    ]
    assert view["confidence_basis"] == "MODEL_SELF_REPORT_UNCALIBRATED"
    assert not view["standalone_trigger_allowed"]
    assert view["result"]["formula"] == "CREDIBILITY_SOURCE_CONFIDENCE_RECENCY_V2"
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await score(journal_client)).json() == view
    previous_time = get_clock().utcnow()
    fake_clock.advance(timedelta(seconds=1))
    disabled = policy_request(enabled=False, expected=configured.json()["event_id"])
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=disabled)
    ).status_code == 200
    assert (await score(journal_client)).json()["status"] == "UNAVAILABLE"
    assert (await score(journal_client, as_of=previous_time.isoformat())).json() == view
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=disabled)
    ).status_code == 409
    journal_client.headers.pop("Authorization")
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=disabled)
    ).status_code == 401


@pytest.mark.parametrize(
    "direction,confidence,reason",
    [
        ("UNKNOWN", "1", "DIRECTION_UNAVAILABLE"),
        ("POSITIVE", "0.1", "LOW_CONFIDENCE"),
    ],
)
async def test_unknown_or_low_confidence_never_becomes_zero(
    journal_client, monkeypatch, direction, confidence, reason
):
    await news_context()
    identifier = await seed(
        journal_client, monkeypatch, "fixture", direction, confidence=confidence
    )
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=policy_request())
    ).status_code == 200
    result = (await score(journal_client)).json()
    assert result["status"] == "UNAVAILABLE" and result["result"]["score"] is None
    assert result["result"]["excluded"][identifier] == reason


async def test_shared_strategy_and_pipeline_reject_forged_or_missing_sentiment(
    journal_client, monkeypatch
):
    context, _strategy = await news_context()
    spec = context.strategy.model_copy(
        update={"version": "sentiment", "required_inputs": ("candles", "news", "sentiment")}
    )

    class ReadingStrategy(FixtureStrategy):
        def entry(self, strategy_context):
            assert strategy_context.sentiment.score == 1
            return super().entry(strategy_context)

    strategy = ReadingStrategy(spec)
    await StrategyRegistry().register(strategy, enabled_paper=True)
    context = context.model_copy(update={"strategy": spec})
    identifier = await seed(journal_client, monkeypatch, "positive", "POSITIVE")
    offered = available()
    offered["sentiment"] = offered["candles"].model_copy(update={"value": {"score": 999}})
    assert (
        await evaluate(strategy, available=offered)
    ).reason == "NEWS_SENTIMENT_POLICY_UNAVAILABLE"
    request = payload(evidence=[{"kind": "NEWS", "source_id": identifier}])
    assert (
        await quant_decision(DecisionPipeline(validator()), request, context)
    ).code == "NEWS_SENTIMENT_POLICY_UNAVAILABLE"
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=policy_request())
    ).status_code == 200
    evaluated = await evaluate(strategy, available=offered)
    assert evaluated.reason == "SIGNAL" and evaluated.news_sentiment["result"]["score"] == "1"
    decision = await quant_decision(DecisionPipeline(validator()), request, context)
    assert decision.code == "RISK_APPROVED"
    async with db_session.session_scope() as session:
        proposal = await session.get(Proposal, decision.proposal_id)
        stored = proposal.context_snapshot["news_sentiment"]
        assert stored["policy"]["event_id"] == evaluated.news_sentiment["policy"]["event_id"]
        assert stored["admission_ids"] == [identifier]
    await seed(journal_client, monkeypatch, "second", "POSITIVE")
    missing_evidence = await quant_decision(DecisionPipeline(validator()), request, context)
    assert missing_evidence.code == "SENTIMENT_EVIDENCE_REQUIRED"


async def test_policy_requires_explicit_uncalibrated_score_consent(journal_client):
    request = policy_request()
    request["policy"]["accept_uncalibrated_model_scores"] = False
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=request)
    ).status_code == 422
    current = await journal_client.get("/api/v1/news/sentiment-policy")
    assert current.json()["policy"] is None


async def test_future_stale_and_wrong_origin_cannot_score(journal_client, fake_clock, monkeypatch):
    await news_context()
    await seed(journal_client, monkeypatch, "positive", "POSITIVE")
    assert (
        await journal_client.put("/api/v1/news/sentiment-policy", json=policy_request())
    ).status_code == 200
    future = (get_clock().utcnow() + timedelta(seconds=1)).isoformat()
    assert (await score(journal_client, as_of=future)).status_code == 422
    assert (await score(journal_client, origin="LIVE")).json()["status"] == "UNAVAILABLE"
    fake_clock.advance(timedelta(seconds=181))
    stale = await sentiment_at(
        "ins-test", as_of=get_clock().utcnow(), origin="SYNTHETIC", max_age_seconds=180
    )
    assert stale.status == "UNAVAILABLE" and stale.result.score is None
