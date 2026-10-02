"""Server research resolution and independent shared-pipeline refusal of unsafe inputs."""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditIdentity, AuditService
from app.config import reload_settings
from app.core.enums import VerificationStatus
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.modes import TradingMode
from app.news.availability import research_at
from app.news.polling import poll_chain
from app.strategies.registry import StrategyRegistry
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_news_verification import story
from tests.integration.test_pipeline import setup_context, time_control
from tests.integration.test_proposal import validator
from tests.integration.test_strategies import evaluate
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy, available

__all__ = ["credentials", "journal_client", "time_control"]


async def news_context():
    context = await setup_context()
    spec = context.strategy.model_copy(
        update={"version": "news", "required_inputs": ("candles", "news")}
    )
    strategy = FixtureStrategy(spec)
    await StrategyRegistry().register(strategy, enabled_paper=True)
    return context.model_copy(update={"strategy": spec}), strategy


@pytest.mark.parametrize(
    "case",
    [
        "primary",
        "single",
        "future_fetch",
        "stale",
        "conflict",
        "origin",
        "disabled",
        "future_config",
        "unverified",
    ],
)
async def test_news_declaration_requires_real_admissible_records(db_engine, case):
    context, strategy = await news_context()
    await story(case)
    if case == "unverified":
        async with db_session.session_scope() as session:
            (
                await session.get(NewsItem, "news-test")
            ).verification_status = VerificationStatus.UNVERIFIED
    offered = available()
    offered["news"] = offered["candles"].model_copy(
        update={"value": {"verified": True, "score": 1}}
    )
    evaluated = await evaluate(strategy, available=offered)
    assert evaluated.reason == ("SIGNAL" if case == "primary" else "NEWS_RESEARCH_UNAVAILABLE")
    assert strategy.calls == int(case == "primary")
    if case == "primary":
        assert evaluated.news_evidence[0]["source_id"] == "news-test"
        assert evaluated.news_evidence[0]["publisher_count"] == 1
    result = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": "news-test"}]),
        context,
    )
    assert result.code == ("RISK_APPROVED" if case == "primary" else "NEWS_RESEARCH_UNAVAILABLE")


async def test_missing_citation_and_disabled_news_cannot_be_bypassed(db_engine, monkeypatch):
    context, _strategy = await news_context()
    await story("primary")
    result = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert result.code == "NEWS_EVIDENCE_REQUIRED"
    monkeypatch.setenv("NEWS_ENABLED", "false")

    reload_settings()
    result = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert result.code == "NEWS_SERVICE_DISABLED"


async def test_future_failure_cannot_change_past_research_but_current_failure_blocks(
    db_engine, fake_clock
):
    await setup_context()
    await story("primary")
    before = await research_at("ins-test", as_of=OBSERVED, origin="SYNTHETIC", max_age_seconds=60)
    assert before.status == "AVAILABLE"
    fake_clock.advance(timedelta(seconds=1))
    await AuditService().append(
        AuditIdentity(
            chain_id=poll_chain("test-exchange"),
            event_type="NEWS_POLL_STARTED",
            actor="test",
            mode=TradingMode.PAPER,
        ),
        {"result": {"source_slug": "test-exchange"}},
    )
    historical = await research_at(
        "ins-test", as_of=OBSERVED, origin="SYNTHETIC", max_age_seconds=60
    )
    assert historical == before
    current = await research_at(
        "ins-test", as_of=fake_clock.now(), origin="SYNTHETIC", max_age_seconds=60
    )
    assert current.status == "UNAVAILABLE"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NewsItem)) == 1


async def test_news_storage_failure_blocks_only_declared_dependency(db_engine, monkeypatch):
    context, _strategy = await news_context()
    original = AsyncSession.scalars

    async def failed_news_read(self, statement, *args, **kwargs):
        if any(
            item.get("entity") is NewsItem for item in getattr(statement, "column_descriptions", [])
        ):
            raise SQLAlchemyError("Isolated news table failure")
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalars", failed_news_read)
    dependent = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert dependent.code == "NEWS_SERVICE_DEGRADED" and dependent.approved_quantity == 0
    independent = context.model_copy(update={"strategy": FixtureStrategy().spec})
    result = await quant_decision(DecisionPipeline(validator()), payload(), independent)
    assert result.code == "RISK_APPROVED"


async def test_authenticated_research_api_exposes_only_admitted_evidence(journal_client):
    await setup_context()
    await story("primary")
    response = await journal_client.get(
        "/api/v1/news/research/ins-test", params={"origin": "SYNTHETIC"}
    )
    assert response.status_code == 200 and response.json()["status"] == "AVAILABLE"
    assert response.json()["articles"][0]["source_id"] == "news-test"
    assert (
        await journal_client.get(
            "/api/v1/news/research/ins-test", params={"as_of": "2099-01-01T00:00:00Z"}
        )
    ).status_code == 422
    journal_client.headers.pop("Authorization")
    assert (await journal_client.get("/api/v1/news/research/ins-test")).status_code == 401
