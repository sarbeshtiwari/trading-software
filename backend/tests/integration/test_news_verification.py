"""Configured publishers, not flags or source counts, justify stored news evidence."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.agents.pipeline import DecisionPipeline
from app.core.clock import UTC
from app.core.enums import VerificationStatus
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.news import NewsItem, NewsSource
from app.news.sentiment_store import stored_news_sentiment
from tests.conftest import BACKEND_ROOT
from tests.integration.test_journal_api import credentials, journal_client
from tests.integration.test_pipeline import setup_context, time_control
from tests.integration.test_proposal import validator
from tests.news_fixture import source
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload
from tests.unit.test_sentiment import policy

__all__ = ["credentials", "journal_client", "time_control"]


async def story(case):
    timestamp = OBSERVED.astimezone(UTC)
    async with db_session.session_scope() as session:
        primary = case == "primary"
        first = source(session, tier=1 if primary else 2, kind="filings" if primary else "rss")
        attributions = [first]
        if not primary and case != "single":
            attributions.append(
                source(
                    session,
                    slug="other",
                    publisher="FIXTURE-EXCHANGE" if case == "aliases" else "other-publisher",
                    tier=2,
                    kind="rss",
                )
            )
        row = NewsItem(
            id="news-test",
            dedupe_key="isolated-story",
            title="Isolated results fixture",
            body="Recorded company results increased by 10 percent.",
            url=first["url"],
            data_origin="SYNTHETIC",
            published_at=timestamp,
            fetched_at=timestamp,
            verification_status=VerificationStatus.VERIFIED,
            sources=attributions,
            best_tier=1,
            source_count=999,
            entities=[{"instrument_id": "ins-test", "symbol": "TEST"}],
            interpretation={"confidence": "0.8"},
            sentiment_score=Decimal("0.5"),
        )
        session.add(row)
        await session.flush()
        configured = await session.get(NewsSource, "source-test-exchange")
        if case == "disabled":
            configured.is_enabled = False
        elif case == "unconfigured":
            configured.publisher_id = None
        elif case == "future_config":
            configured.updated_at = timestamp + timedelta(seconds=1)
        elif case == "future_fetch":
            row.sources = [
                first | {"fetched_at": (timestamp + timedelta(seconds=1)).isoformat()},
                *attributions[1:],
            ]
        elif case == "wrong_host":
            row.sources = [
                first | {"url": "https://unregistered.example.test/story"},
                *attributions[1:],
            ]
        elif case == "duplicate":
            row.sources = [first, first]
        elif case == "conflict":
            row.conflicts_with = ["other-report"]
        elif case == "stale":
            row.published_at = timestamp - timedelta(days=3)
        elif case == "origin":
            row.data_origin = "LIVE"
        elif case == "missing_origin":
            row.data_origin = None
        elif case == "malformed":
            row.sources = [{"slug": "test-exchange", "fetched_at": "not-a-date"}]
        elif case == "empty_body":
            row.body = None
        elif case == "fake_primary":
            row.sources = [first]
            configured.tier = 1
            configured.kind = "rss"
        elif case == "malformed_entities":
            row.entities = [None]


@pytest.mark.parametrize(
    "case",
    [
        "primary",
        "independent",
        "single",
        "aliases",
        "disabled",
        "unconfigured",
        "future_config",
        "future_fetch",
        "wrong_host",
        "duplicate",
        "conflict",
        "stale",
        "origin",
        "missing_origin",
        "malformed",
        "empty_body",
        "fake_primary",
        "malformed_entities",
    ],
)
async def test_shared_pipeline_and_sentiment_require_corroboration(db_engine, case):
    context = await setup_context()
    await story(case)
    result = await quant_decision(
        DecisionPipeline(validator()),
        payload(evidence=[{"kind": "NEWS", "source_id": "news-test"}]),
        context,
    )
    sentiment = await stored_news_sentiment(
        "TEST", as_of=OBSERVED, policy=policy(), origin="SYNTHETIC"
    )
    usable = case in {"primary", "independent"}
    assert result.code == ("RISK_APPROVED" if usable else "EVIDENCE_UNRESOLVABLE")
    assert sentiment.score == (Decimal("0.5") if usable else None)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == int(usable)
        if usable:
            proposal = await session.get(Proposal, result.proposal_id)
            snapshot = proposal.context_snapshot["validated_evidence"]["NEWS:news-test"]
            assert snapshot["publisher_count"] == (1 if case == "primary" else 2)
            assert snapshot["best_tier"] == (1 if case == "primary" else 2)


def test_news_migration_handles_legacy_schema_without_fabricating_provenance(
    settings_env, tmp_path
):
    path = tmp_path / "legacy-news.db"
    settings_env(DATABASE_URL=f"sqlite+aiosqlite:///{path}")
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "0011_llm_budget")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            assert "data_origin" not in {
                column["name"] for column in sa.inspect(connection).get_columns("news_items")
            }
            legacy = sa.Table("news_sources", sa.MetaData(), autoload_with=connection)
            connection.execute(
                sa.insert(legacy),
                {
                    "id": "legacy",
                    "slug": "legacy",
                    "name": "Legacy fixture",
                    "tier": 2,
                    "kind": "rss",
                    "weight": 1,
                    "is_enabled": True,
                    "created_at": OBSERVED,
                },
            )
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.scalar(sa.select(NewsSource.publisher_id)) is None
            connection.execute(sa.update(NewsSource).values(publisher_id="owner-identified"))
        with pytest.raises(RuntimeError, match="Cannot discard recorded news provenance"):
            command.downgrade(config, "0011_llm_budget")
    finally:
        engine.dispose()


async def test_unverified_news_rejection_is_visible_without_stopping_quant(journal_client):
    context = await setup_context()
    await story("single")
    pipeline = DecisionPipeline(validator())
    denied = await quant_decision(
        pipeline, payload(evidence=[{"kind": "NEWS", "source_id": "news-test"}]), context
    )
    assert denied.code == "EVIDENCE_UNRESOLVABLE"
    independent = await quant_decision(pipeline, payload(), context)
    assert independent.code == "RISK_APPROVED"
    view = (await journal_client.get("/api/v1/workspace")).json()
    assert any(
        row["id"] == denied.candidate_id and row["reason_code"] == denied.code
        for row in view["decisions"]
    )
    entries = (await journal_client.get("/api/v1/journal?kind=REJECTION")).json()["entries"]
    assert any(row["rejection_code"] == denied.code for row in entries)
    assert view["orders"] == []
