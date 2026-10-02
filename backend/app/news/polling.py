"""Durably rate-limited acquisition with atomic article batches and sanitized failures."""

import asyncio
from datetime import timedelta
from hashlib import sha256
from itertools import pairwise
from typing import Literal

import sqlalchemy as sa

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.enums import Severity
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsSource
from app.modes import TradingMode
from app.news.ingest import ArticleReceipt, import_article, utc
from app.news.providers import NewsProvider, RemoteNewsProvider
from app.news.sources import describe
from app.notifications.outbox import enqueue


class PollResult(EvidenceModel):
    attempt_id: str
    status: Literal["ACQUIRED_UNVERIFIED", "EMPTY", "DEGRADED"]
    articles: list[ArticleReceipt]


class PollState(EvidenceModel):
    status: Literal["NOT_POLLED", "PENDING", "DEGRADED", "ACQUIRED_UNVERIFIED", "EMPTY", "STALE"]
    result: PollResult | None = None


async def acquisition_state(session, slug):
    history = await poll_history(session, slug)
    if not history:
        return PollState(status="NOT_POLLED")
    now = get_clock().utcnow()
    last = history[-1]
    if utc(last.occurred_at) > now:
        raise ValueError("Future acquisition audit")
    if last.event_type == "NEWS_POLL_STARTED":
        return PollState(
            status="PENDING" if now - utc(last.occurred_at) < timedelta(seconds=20) else "DEGRADED"
        )
    if last.event_type != "NEWS_POLL_FINISHED" or len(history) < 2:
        raise ValueError("Acquisition audit invalid")
    result = PollResult.model_validate(last.result)
    previous = history[-2]
    if previous.event_type != "NEWS_POLL_STARTED" or result.attempt_id != previous.id:
        raise ValueError("Acquisition receipt mismatch")
    source = await session.scalar(sa.select(NewsSource).where(NewsSource.slug == slug))
    view = await describe(session, source) if source else None
    if (
        view is None
        or view.event_id != previous.result["source_event_id"]
        or not view.configuration.is_enabled
        or now - utc(last.occurred_at)
        > timedelta(seconds=get_settings().news_poll_interval_seconds)
    ):
        return PollState(status="STALE", result=result)
    return PollState(status=result.status, result=result)


def poll_chain(slug):
    return "news-poll-" + sha256(slug.encode()).hexdigest()[:30]


async def poll_history(session, slug):
    history = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == poll_chain(slug))
                .order_by(AuditEvent.sequence)
                .with_for_update()
            )
        ).all()
    )
    if not verify_records(history) or any(
        utc(previous.occurred_at) > utc(current.occurred_at)
        for previous, current in pairwise(history)
    ):
        raise ValueError("News acquisition audit corrupt")
    return history


class NewsPoller:
    def __init__(self, provider: NewsProvider | None = None, *, clock=None):
        self.provider = provider or RemoteNewsProvider()
        self.clock = clock or get_clock()

    async def poll(self, slug, *, expected_event_id, actor):
        settings = get_settings()
        interval = settings.news_poll_interval_seconds
        if not settings.news_enabled or interval < 30:
            raise ValueError("News acquisition disabled or interval invalid")
        async with db_session.session_scope() as session:
            source = await session.scalar(
                sa.select(NewsSource).where(NewsSource.slug == slug).with_for_update()
            )
            if source is None:
                raise ValueError("News source unavailable")
            view = await describe(session, source)
            if (
                view.integrity != "AUDITED"
                or view.event_id != expected_event_id
                or not view.configuration.is_enabled
                or view.configuration.kind not in {"rss", "api"}
                or utc(source.updated_at or source.created_at) > self.clock.utcnow()
            ):
                raise ValueError("News source not eligible for acquisition")
            history = await poll_history(session, slug)
            if history and self.clock.utcnow() < utc(history[-1].occurred_at) + timedelta(
                seconds=interval
            ):
                raise ValueError("News source rate limited")
            count = len(history)
            attempt = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=poll_chain(slug),
                    event_type="NEWS_POLL_STARTED",
                    actor=actor,
                    mode=TradingMode.PAPER,
                ),
                {"result": {"source_event_id": view.event_id, "source_slug": slug}},
                expected_count=count,
            )
            attempt_id = attempt.id
        try:
            articles = await asyncio.wait_for(
                self.provider.articles(view.configuration), timeout=20
            )
            if not isinstance(articles, list) or len(articles) > 100:
                raise ValueError("Invalid article batch")
            async with db_session.session_scope() as session:
                current = await session.scalar(
                    sa.select(NewsSource).where(NewsSource.slug == slug).with_for_update()
                )
                latest = await describe(session, current) if current is not None else None
                if (
                    latest is None
                    or latest.event_id != view.event_id
                    or not latest.configuration.is_enabled
                ):
                    raise ValueError("Source changed during acquisition")
                receipts = [
                    await import_article(
                        session,
                        slug,
                        article,
                        expected_event_id=view.event_id,
                        actor=actor,
                        clock=self.clock,
                        acquisition="REMOTE_FEED",
                    )
                    for article in articles
                ]
                receipts = list({receipt.article_id: receipt for receipt in receipts}.values())
                result = PollResult(
                    attempt_id=attempt_id,
                    status="ACQUIRED_UNVERIFIED" if receipts else "EMPTY",
                    articles=receipts,
                )
                await self._finish(session, slug, actor, count, result)
                return result
        except asyncio.CancelledError:
            raise
        except Exception:
            result = PollResult(attempt_id=attempt_id, status="DEGRADED", articles=[])
            async with db_session.session_scope() as session:
                await self._finish(session, slug, actor, count, result)
            return result

    async def _finish(self, session, slug, actor, count, result):
        history = await poll_history(session, slug)
        if (
            len(history) != count + 1
            or history[-1].id != result.attempt_id
            or self.clock.utcnow() < utc(history[-1].occurred_at)
        ):
            raise ValueError("Acquisition version or clock changed")
        event = await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=poll_chain(slug),
                event_type="NEWS_POLL_FINISHED",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {"result": result.model_dump(mode="json")},
            expected_count=count + 1,
        )
        if result.status == "DEGRADED":
            await enqueue(
                session,
                key=f"news-degraded:{result.attempt_id}",
                event_type="NEWS_DEGRADED",
                severity=Severity.WARNING,
                message=f"NEWS SERVICE DEGRADED: {slug}",
                clock=self.clock,
                source_event=event,
            )
