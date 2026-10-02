"""Authenticated source controls; configuration is not an ingestion health claim."""

from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import AwareDatetime, Field, field_validator
from sqlalchemy.exc import IntegrityError, OperationalError

from app.analysis.equity import EvidenceModel
from app.api.risk import _paper_only
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.news import NewsSource
from app.news.availability import NewsResearch, research_at
from app.news.conflicts import ConflictView, read_conflicts
from app.news.dedupe import StoryView, article_story
from app.news.entities import AttributionView, attribute, read_attribution
from app.news.ingest import ArticleInput, ArticleReceipt, ArticleView, import_article, read_article
from app.news.interpret import InterpretationView, interpret_article, read_interpretation
from app.news.polling import NewsPoller, PollResult, PollState, acquisition_state
from app.news.production import ProductionResult
from app.news.production_controls import ProductionControl, control_job, read_job
from app.news.production_state import ProductionView
from app.news.reactions import admit
from app.news.research import AdmissionView
from app.news.research_sentiment import (
    AdvisorySentiment,
    AdvisorySentimentPolicy,
    SentimentPolicyView,
    configure_policy,
    policy_at,
    sentiment_at,
)
from app.news.sources import SourcePolicy, SourceView, configure, describe

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/research/{instrument_id}", response_model=NewsResearch)
async def instrument_research(
    instrument_id: str,
    as_of: AwareDatetime | None = None,
    origin: DataOrigin = DataOrigin.LIVE,
    max_age_seconds: int = Query(3600, ge=1, le=86400),
):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future research read is not permitted")
    return await research_at(
        instrument_id, as_of=cutoff, origin=origin, max_age_seconds=max_age_seconds
    )


class NewsRuntimeView(EvidenceModel):
    status: Literal[
        "DISABLED", "UNAVAILABLE", "STARTING", "DEGRADED", "IDLE", "RUNNING", "STOPPED", "STALE"
    ]
    running: bool
    last_cycle: AwareDatetime | None
    processed_last_cycle: int
    automatic_enabled: bool
    actionable_news: Literal["UNAVAILABLE", "PER_INSTRUMENT_CHECK_REQUIRED"] = Field(
        description="Automatic research production only; resolve instrument research separately."
    )
    research_enabled: bool = False
    research_status: Literal[
        "DISABLED", "UNAVAILABLE", "DEGRADED", "RUNNING", "IDLE", "STOPPED"
    ] = "DISABLED"
    research_results: list[ProductionResult] = Field(default_factory=list)


@router.get("/runtime", response_model=NewsRuntimeView)
async def news_runtime(request: Request):
    return NewsRuntimeView.model_validate(request.app.state.news_runtime.snapshot())


@router.get("/production/{group_event_id}", response_model=ProductionView)
async def production_job(group_event_id: str = Path(min_length=1, max_length=40)):
    try:
        async with db_session.session_scope() as session:
            return await read_job(session, group_event_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Research job unavailable or integrity invalid") from None


@router.post("/production/{group_event_id}/control", response_model=ProductionView)
async def production_control(
    body: ProductionControl,
    request: Request,
    group_event_id: str = Path(min_length=1, max_length=40),
):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await control_job(
                session, group_event_id, body, actor=request.state.principal.username
            )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "Research recovery unavailable, ineligible or concurrently changed"
        ) from None


class ConfigureSource(EvidenceModel):
    configuration: SourcePolicy
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if len(value) < 10:
            raise ValueError("A meaningful source configuration reason is required")
        return value


class SourceList(EvidenceModel):
    sources: list[SourceView]
    has_more: bool


class ImportArticle(EvidenceModel):
    article: ArticleInput
    expected_event_id: str = Field(min_length=1, max_length=40)


class PollRequest(EvidenceModel):
    expected_event_id: str = Field(min_length=1, max_length=40)


@router.get("/sources/{slug}/acquisition", response_model=PollState)
async def source_acquisition(slug: str = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")):
    try:
        async with db_session.session_scope() as session:
            return await acquisition_state(session, slug)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "News acquisition state unavailable") from None


@router.post("/sources/{slug}/poll", response_model=PollResult)
async def poll_source(
    body: PollRequest, request: Request, slug: str = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
):
    _paper_only()
    try:
        return await NewsPoller().poll(
            slug, expected_event_id=body.expected_event_id, actor=request.state.principal.username
        )
    except (ValueError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "News source unavailable, rate limited or concurrently changed"
        ) from None


@router.get("/articles/{article_id}", response_model=ArticleView)
async def article_observation(article_id: str, as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future article read is not permitted")
    try:
        async with db_session.session_scope() as session:
            result = await read_article(session, article_id, as_of=cutoff)
            if result is None:
                raise HTTPException(404, "Article unavailable at requested timestamp")
            return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Article integrity unavailable") from None


class SentimentPolicyChange(EvidenceModel):
    policy: AdvisorySentimentPolicy
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)


@router.get("/sentiment-policy", response_model=SentimentPolicyView)
async def news_sentiment_policy(as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future sentiment policy read is not permitted")
    try:
        async with db_session.session_scope() as session:
            return await policy_at(session, cutoff)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Sentiment policy integrity unavailable") from None


@router.put("/sentiment-policy", response_model=SentimentPolicyView)
async def configure_news_sentiment(body: SentimentPolicyChange, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await configure_policy(
                session,
                body.policy,
                expected_event_id=body.expected_event_id,
                actor=request.state.principal.username,
                reason=body.reason,
            )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "Sentiment policy invalid, unavailable or concurrently changed"
        ) from None


@router.get("/sentiment/{instrument_id}", response_model=AdvisorySentiment)
async def news_advisory_sentiment(
    instrument_id: str,
    as_of: AwareDatetime | None = None,
    origin: DataOrigin = DataOrigin.LIVE,
    max_age_seconds: int = Query(3600, ge=1, le=86400),
):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future sentiment read is not permitted")
    return await sentiment_at(
        instrument_id, as_of=cutoff, origin=origin, max_age_seconds=max_age_seconds
    )


class AdmitResearch(EvidenceModel):
    instrument_id: str = Field(min_length=1, max_length=40)
    interpretation_event_id: str = Field(min_length=1, max_length=40)


@router.post("/articles/{article_id}/research", response_model=AdmissionView)
async def admit_research(article_id: str, body: AdmitResearch, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await admit(
                session,
                article_id,
                body.instrument_id,
                body.interpretation_event_id,
                actor=request.state.principal.username,
            )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "Research source policy, attribution or evidence unavailable"
        ) from None


class InterpretRequest(EvidenceModel):
    expected_event_id: str = Field(min_length=1, max_length=40)


@router.post("/articles/{article_id}/interpretation", response_model=InterpretationView)
async def interpret_news_article(article_id: str, body: InterpretRequest, request: Request):
    _paper_only()
    try:
        return await interpret_article(
            article_id,
            expected_event_id=body.expected_event_id,
            actor=request.state.principal.username,
        )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(409, "News interpretation unavailable or evidence changed") from None


@router.get("/articles/{article_id}/interpretation", response_model=InterpretationView)
async def news_interpretation(article_id: str, as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future interpretation read is not permitted")
    try:
        async with db_session.session_scope() as session:
            result = await read_interpretation(session, article_id, as_of=cutoff)
            if result is None:
                raise HTTPException(404, "Interpretation unavailable at requested timestamp")
            return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "News interpretation integrity unavailable") from None


@router.get("/articles/{article_id}/conflicts", response_model=ConflictView)
async def article_conflicts(article_id: str, as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future conflict read is not permitted")
    try:
        async with db_session.session_scope() as session:
            story = await article_story(session, article_id, as_of=cutoff)
            result = None if story is None else await read_conflicts(session, story, as_of=cutoff)
            if result is None:
                raise HTTPException(404, "Conflict assessment unavailable")
            for member in story.story.members:
                if await read_article(session, member.article_id, as_of=cutoff) is None:
                    raise ValueError("Conflict source unavailable")
            return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Conflict assessment integrity unavailable") from None


@router.get("/articles/{article_id}/story", response_model=StoryView)
async def article_story_group(article_id: str, as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future story read is not permitted")
    try:
        async with db_session.session_scope() as session:
            result = await article_story(session, article_id, as_of=cutoff)
            if result is None:
                raise HTTPException(404, "Story grouping unavailable at requested timestamp")
            for member in result.story.members:
                if await read_article(session, member.article_id, as_of=cutoff) is None:
                    raise ValueError("Story member unavailable")
            return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Story grouping integrity unavailable") from None


@router.get("/articles/{article_id}/entities", response_model=AttributionView)
async def article_entities(article_id: str, as_of: AwareDatetime | None = None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff > get_clock().utcnow():
        raise HTTPException(422, "Future attribution read is not permitted")
    try:
        async with db_session.session_scope() as session:
            article = await read_article(session, article_id, as_of=cutoff)
            result = (
                None
                if article is None
                else await read_attribution(
                    session, article_id, article.receipt.audit_id, as_of=cutoff
                )
            )
            if result is None:
                raise HTTPException(404, "Attribution unavailable at requested timestamp")
            return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, "Entity attribution integrity unavailable") from None


@router.post("/articles/{article_id}/entities", response_model=AttributionView)
async def refresh_article_entities(article_id: str, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            article = await read_article(session, article_id, as_of=get_clock().utcnow())
            if article is None:
                raise HTTPException(404, "Article unavailable")
            await attribute(
                session,
                article_id,
                article.receipt.audit_id,
                article.article.title,
                article.article.body,
                actor=request.state.principal.username,
            )
            return await read_attribution(
                session, article_id, article.receipt.audit_id, as_of=get_clock().utcnow()
            )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(409, "Entity attribution unavailable or concurrently changed") from None


@router.post("/sources/{slug}/articles", response_model=ArticleReceipt)
async def ingest_article(
    body: ImportArticle,
    request: Request,
    slug: str = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await import_article(
                session,
                slug,
                body.article,
                expected_event_id=body.expected_event_id,
                actor=request.state.principal.username,
            )
    except (ValueError, KeyError, TypeError, AttributeError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "Article import unavailable, invalid or concurrently changed"
        ) from None


@router.get("/sources", response_model=SourceList)
async def sources():
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.scalars(sa.select(NewsSource).order_by(NewsSource.slug).limit(201))
            ).all()
        )
        return SourceList(
            sources=[await describe(session, row) for row in rows[:200]], has_more=len(rows) > 200
        )


@router.put("/sources/{slug}", response_model=SourceView)
async def configure_source(
    body: ConfigureSource,
    request: Request,
    slug: str = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await configure(
                session,
                slug,
                body.configuration,
                expected_event_id=body.expected_event_id,
                actor=request.state.principal.username,
                reason=body.reason,
            )
    except (ValueError, IntegrityError, OperationalError):
        raise HTTPException(
            409, "Source configuration unavailable, corrupt or concurrently changed"
        ) from None


@router.get("/sources/{slug}", response_model=SourceView)
async def source(slug: str = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")):
    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(NewsSource).where(NewsSource.slug == slug))
        if row is None:
            raise HTTPException(404, "Source not configured")
        return await describe(session, row)
