"""Acquisition failure must block direct citations and sentiment, not only declared strategies."""

from datetime import timedelta

import pytest

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditIdentity, AuditService
from app.config import reload_settings
from app.core.clock import get_clock
from app.modes import TradingMode
from app.news.polling import poll_chain
from app.news.sentiment_store import stored_news_sentiment
from tests.integration.test_news_verification import story
from tests.integration.test_pipeline import setup_context, time_control
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_sentiment import policy

__all__ = ["time_control"]


@pytest.mark.parametrize("status", ["DEGRADED", "PENDING", "EMPTY", "STALE", "DISABLED"])
async def test_all_news_consumers_fail_closed_but_independent_quant_continues(
    db_engine,
    fake_clock,
    monkeypatch,
    status,
):
    context = await setup_context()
    await story("primary")
    now = get_clock().utcnow()
    if status == "DISABLED":
        monkeypatch.setenv("NEWS_ENABLED", "false")
        reload_settings()
    else:
        if status == "STALE":
            fake_clock.set_to(now - timedelta(seconds=301))
        await AuditService().append(
            AuditIdentity(
                chain_id=poll_chain("test-exchange"),
                event_type="NEWS_POLL_STARTED" if status == "PENDING" else "NEWS_POLL_FINISHED",
                actor="isolated_fixture",
                mode=TradingMode.PAPER,
            ),
            {"result": {"status": "ACQUIRED_UNVERIFIED" if status == "STALE" else status}},
        )
        fake_clock.set_to(now)
    cited = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": "news-test"}]),
        context,
    )
    assert cited.code == "EVIDENCE_UNRESOLVABLE" and cited.approved_quantity == 0
    sentiment = await stored_news_sentiment(
        "TEST", as_of=context.market.as_of, policy=policy(), origin="SYNTHETIC"
    )
    assert sentiment.score is None and sentiment.contributions == ()
    independent = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert independent.code == "RISK_APPROVED"


async def test_future_failure_does_not_poison_historical_citation(db_engine, fake_clock):
    context = await setup_context()
    await story("primary")
    fake_clock.advance(timedelta(seconds=1))
    await AuditService().append(
        AuditIdentity(
            chain_id=poll_chain("test-exchange"),
            event_type="NEWS_POLL_FINISHED",
            actor="isolated_fixture",
            mode=TradingMode.PAPER,
        ),
        {"result": {"status": "DEGRADED"}},
    )
    cited = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": "news-test"}]),
        context,
    )
    assert cited.code == "RISK_APPROVED"
