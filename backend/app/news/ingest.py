"""Immutable owner-imported article observations; imports never confer verification."""

import json
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, field_validator

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import VerificationStatus
from app.core.ids import new_id
from app.core.logging import redact_data
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsItem, NewsSource
from app.modes import TradingMode
from app.news.conflicts import assess_story
from app.news.dedupe import group_story
from app.news.entities import attribute
from app.news.sources import describe


class ArticleInput(EvidenceModel):
    url: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=100_000)
    published_at: AwareDatetime
    data_origin: DataOrigin

    @field_validator("title", "body")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Article text must not be blank")
        return value


class ArticleReceipt(EvidenceModel):
    article_id: str
    audit_id: str
    observed_at: AwareDatetime
    acquisition: Literal["OWNER_IMPORT", "REMOTE_FEED"] = "OWNER_IMPORT"
    verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"


class ArticleView(EvidenceModel):
    receipt: ArticleReceipt
    article: ArticleInput
    source_slug: str
    source_event_id: str


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def article_chain(article_id):
    return "article-" + sha256(article_id.encode()).hexdigest()[:32]


async def read_article(session, article_id, *, as_of):
    row = await session.get(NewsItem, article_id)
    if row is None or utc(row.created_at) > as_of:
        return None
    history = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == article_chain(article_id))
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if len(history) != 1 or not verify_records(history):
        raise ValueError("Article audit unavailable")
    event = history[0]
    sealed = event.result
    article = ArticleInput.model_validate(sealed["article"])
    observed = utc(event.occurred_at)
    expected_key = sha256(
        json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        event.event_type != "NEWS_ARTICLE_IMPORTED"
        or event.mode != TradingMode.PAPER
        or sealed["acquisition"] not in {"OWNER_IMPORT", "REMOTE_FEED"}
        or row.dedupe_key != expected_key
        or row.title != article.title
        or row.body != article.body
        or row.url != article.url
        or row.data_origin != article.data_origin.value
        or utc(row.published_at) != article.published_at
        or not article.published_at <= observed <= as_of
        or utc(row.fetched_at) != observed
        or utc(row.created_at) != observed
        or utc(row.updated_at) != observed
        or row.sources
        != [{"slug": sealed["source_slug"], "url": article.url, "fetched_at": observed.isoformat()}]
        or row.best_tier != sealed["source_policy"]["tier"]
        or row.source_count != 1
        or row.verification_status != VerificationStatus.UNVERIFIED
        or row.entities
        or row.interpretation
        or row.conflicts_with
    ):
        raise ValueError("Article observation integrity failure")
    return ArticleView(
        receipt=ArticleReceipt(
            article_id=row.id,
            audit_id=event.id,
            observed_at=observed,
            acquisition=sealed["acquisition"],
        ),
        article=article,
        source_slug=sealed["source_slug"],
        source_event_id=sealed["source_event_id"],
    )


async def import_article(
    session, slug, article, *, expected_event_id, actor, clock=None, acquisition="OWNER_IMPORT"
):
    if acquisition not in {"OWNER_IMPORT", "REMOTE_FEED"}:
        raise ValueError("Unknown acquisition method")
    clock = clock or get_clock()
    now = clock.utcnow()
    article = ArticleInput.model_validate(article.model_dump())
    source = await session.scalar(
        sa.select(NewsSource).where(NewsSource.slug == slug).with_for_update()
    )
    if source is None:
        raise ValueError("Source unavailable")
    view = await describe(session, source)
    if (
        view.integrity != "AUDITED"
        or view.event_id != expected_event_id
        or not view.configuration.is_enabled
        or article.published_at > now
    ):
        raise ValueError("Source disabled, changed or article future dated")
    policy = view.configuration
    policy.model_validate({**policy.model_dump(), "endpoint": article.url})
    if urlsplit(article.url).hostname != urlsplit(policy.endpoint).hostname:
        raise ValueError("Article attribution does not match configured source")
    configured = source.updated_at or source.created_at
    configured = configured.replace(tzinfo=UTC) if configured.tzinfo is None else configured
    if configured > now:
        raise ValueError("Source configuration is future dated")
    payload = article.model_dump(mode="json")
    payload["published_at"] = article.published_at.astimezone(UTC).isoformat()
    if redact_data(payload) != payload:
        raise ValueError("Article contains secret-shaped data")
    sealed = {
        "article": payload,
        "source_slug": slug,
        "source_event_id": view.event_id,
        "source_policy": policy.model_dump(mode="json"),
        "acquisition": acquisition,
    }
    key = sha256(json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = await session.scalar(sa.select(NewsItem).where(NewsItem.dedupe_key == key))
    if row is not None:
        archived = await read_article(session, row.id, as_of=now)
        if archived is None:
            raise ValueError("Article observation is future dated")
        return archived.receipt
    row = NewsItem(
        id=new_id("nws"),
        dedupe_key=key,
        url=article.url,
        url_hash=sha256(article.url.encode()).hexdigest(),
        title=article.title,
        body=article.body,
        data_origin=article.data_origin.value,
        published_at=article.published_at,
        fetched_at=now,
        created_at=now,
        updated_at=now,
        sources=[{"slug": slug, "url": article.url, "fetched_at": now.isoformat()}],
        best_tier=policy.tier,
        source_count=1,
        entities=[],
        verification_status=VerificationStatus.UNVERIFIED,
        verification_detail=f"{acquisition}; content is not independently corroborated",
    )
    session.add(row)
    event = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=article_chain(row.id),
            event_type="NEWS_ARTICLE_IMPORTED",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": sealed, "correlation_id": row.id},
        expected_count=0,
    )
    observed = event.occurred_at
    if observed < now:
        raise ValueError("Article observation clock regression")
    row.fetched_at = row.created_at = row.updated_at = observed
    row.sources = [{"slug": slug, "url": article.url, "fetched_at": observed.isoformat()}]
    await session.flush()
    await attribute(session, row.id, event.id, row.title, row.body, actor=actor, clock=clock)
    story_event = await group_story(session, event, actor=actor, clock=clock)
    await assess_story(session, story_event.chain_id, actor=actor, clock=clock)
    return ArticleReceipt(
        article_id=row.id, audit_id=event.id, observed_at=observed, acquisition=acquisition
    )
