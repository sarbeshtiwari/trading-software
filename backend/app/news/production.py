"""Bounded research production with durable pre-call claims and no ambiguous replay."""

import asyncio
from datetime import timedelta
from hashlib import sha256

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.modes import TradingMode
from app.news.dedupe import article_story
from app.news.entities import aware, history
from app.news.interpret import inputs_for, interpret_article, read_interpretation
from app.news.production_state import ProductionResult, job_state
from app.news.reactions import admit
from app.news.readiness import sources_available
from app.news.research import assemble, source_policy


def production_chain(group_event_id):
    return "news-job-" + sha256(group_event_id.encode()).hexdigest()[:30]


class ResearchProducer:
    def __init__(self, settings=None, *, clock=None, origin=DataOrigin.LIVE):
        self.settings = settings or get_settings()
        self.clock = clock or get_clock()
        self.origin = DataOrigin(origin)
        self.cursor = ""
        self.status = "DISABLED"
        self.results = []

    async def cycle(self):
        self.results = []
        if not self.settings.news_research_enabled:
            self.status = "DISABLED"
            return
        if not self.settings.news_enabled or self.settings.trading_mode != TradingMode.PAPER:
            self.status = "UNAVAILABLE"
            return
        now = self.clock.utcnow()
        async with db_session.session_scope() as session:
            identifiers = list(
                (
                    await session.scalars(
                        sa.select(NewsItem.id)
                        .where(
                            NewsItem.id > self.cursor,
                            NewsItem.data_origin == self.origin.value,
                            NewsItem.published_at <= now,
                            NewsItem.published_at
                            >= now - timedelta(hours=self.settings.news_max_age_hours),
                            NewsItem.fetched_at <= now,
                        )
                        .order_by(NewsItem.id)
                        .limit(self.settings.news_research_batch_size)
                    )
                ).all()
            )
        for identifier in identifiers:
            self.cursor = identifier
            try:
                result = await self.produce(identifier)
                if result not in self.results:
                    self.results.append(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.results.append(
                    ProductionResult(
                        article_id=identifier,
                        group_event_id="UNAVAILABLE",
                        status="DEGRADED",
                        code="RESEARCH_PRODUCTION_DEGRADED",
                    )
                )
        if len(identifiers) < self.settings.news_research_batch_size:
            self.cursor = ""
        self.status = (
            "DEGRADED"
            if any(result.status in {"DEGRADED", "RECOVERY_REQUIRED"} for result in self.results)
            else "UNAVAILABLE"
            if any(result.status == "UNAVAILABLE" for result in self.results)
            else "RUNNING"
            if self.results
            else "IDLE"
        )

    async def produce(self, article_id):
        if (
            not self.settings.news_research_enabled
            or not self.settings.news_enabled
            or self.settings.trading_mode != TradingMode.PAPER
            or self.origin not in {DataOrigin.LIVE, DataOrigin.SYNTHETIC}
        ):
            raise ValueError("Automatic research disabled")
        now = self.clock.utcnow()
        async with db_session.session_scope() as session:
            story = await article_story(session, article_id, as_of=now)
            if story is None or story.story.data_origin != self.origin:
                raise ValueError("Research story unavailable")
            sources = []
            for member in story.story.members:
                if (
                    not now - timedelta(hours=self.settings.news_max_age_hours)
                    <= member.published_at
                    <= now
                ):
                    raise ValueError("Research story stale")
                sources.append(await source_policy(session, member, now))
            if not await sources_available(session, sources, now, self.settings):
                raise ValueError("Research source acquisition unavailable")
        inputs = await inputs_for(article_id, now=now, expected_event_id=story.event_id)
        if (
            not inputs.instrument_ids
            or len(inputs.instrument_ids) > 30
            or story.story.ambiguous_group_match
        ):
            return ProductionResult(
                article_id=article_id,
                group_event_id=story.event_id,
                status="UNAVAILABLE",
                code="RESEARCH_SCOPE_UNAVAILABLE",
            )
        chain = production_chain(story.event_id)
        async with db_session.session_scope() as session:
            records = await history(session, chain)
            state = job_state(records, story.event_id, self.clock.utcnow()) if records else None
            if records:
                if state.phase != "RETRY_AUTHORIZED" or state.expired(self.clock.utcnow()):
                    return await self._resume(session, records, story.event_id)
                article_id = state.article_id
            review = None if state else await read_interpretation(session, article_id, as_of=now)
            if review is not None and not review.current_group_matches:
                review = None
            started = await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=chain,
                    event_type="NEWS_PRODUCTION_STARTED",
                    actor="news_scheduler",
                    mode=TradingMode.PAPER,
                ),
                {
                    "result": {
                        "article_id": article_id,
                        "group_event_id": story.event_id,
                        "instrument_ids": inputs.instrument_ids,
                        "authorization_id": state.head.id if state else None,
                        "reuse_interpretation_event_id": review.event_id if review else None,
                    }
                },
                expected_count=len(records),
            )
            started_id = started.id
            job_state([*records, started], story.event_id, self.clock.utcnow())
        return await self._execute(article_id, story.event_id, chain, started_id, review)

    async def _execute(self, article_id, group_event_id, chain, started_id, review):
        try:
            async with db_session.session_scope() as session:
                state = job_state(
                    await history(session, chain), group_event_id, self.clock.utcnow()
                )
                if state.phase == "ABANDONED":
                    return self._stand_down(state)
                if state.phase != "PENDING" or state.started.id != started_id:
                    raise ValueError("Research attempt no longer active")
            if review is None:
                review = await interpret_article(
                    article_id, expected_event_id=group_event_id, actor="news_scheduler"
                )
            async with db_session.session_scope() as session:
                records = await history(session, chain)
                state = job_state(records, group_event_id, self.clock.utcnow())
                if state.phase == "ABANDONED":
                    return self._stand_down(state)
                if state.started.id != started_id:
                    raise ValueError("Research attempt changed")
                result = await self._admissions(session, state.started, review)
                await self._finish(session, chain, result, started_id)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return ProductionResult(
                article_id=article_id,
                group_event_id=group_event_id,
                status="RECOVERY_REQUIRED",
                code="RESEARCH_OUTCOME_UNKNOWN",
            )

    async def _resume(self, session, records, group_event_id):
        state = job_state(records, group_event_id, self.clock.utcnow())
        if state.phase == "FINISHED":
            return state.result
        if state.phase != "PENDING":
            return self._stand_down(state)
        started = state.started
        article_id = started.result["article_id"]
        review = await read_interpretation(session, article_id, as_of=self.clock.utcnow())
        if (
            review is None
            or not review.current_group_matches
            or (
                review.known_at < aware(started.occurred_at)
                and review.event_id != started.result.get("reuse_interpretation_event_id")
            )
        ):
            return ProductionResult(
                article_id=article_id,
                group_event_id=group_event_id,
                status="RECOVERY_REQUIRED",
                code="RESEARCH_OUTCOME_UNKNOWN",
            )
        result = await self._admissions(session, started, review)
        await self._finish(session, started.chain_id, result, started.id)
        return result

    def _stand_down(self, state):
        return ProductionResult(
            article_id=state.article_id,
            group_event_id=state.group_event_id,
            status="ABANDONED" if state.phase == "ABANDONED" else "UNAVAILABLE",
            code="OWNER_ABANDONED_BILLING_UNVERIFIED"
            if state.phase == "ABANDONED"
            else "RETRY_AUTHORIZATION_EXPIRED"
            if state.expired(self.clock.utcnow())
            else "RETRY_AWAITING_SCHEDULER",
        )

    async def _admissions(self, session, started, review):
        base = {
            "article_id": started.result["article_id"],
            "group_event_id": started.result["group_event_id"],
            "interpretation_event_id": review.event_id,
        }
        if (
            not review.current_group_matches
            or review.result.group_event_id != base["group_event_id"]
        ):
            raise ValueError("Research interpretation changed")
        if review.result.status != "GROUNDED_UNVERIFIED":
            return ProductionResult(status="UNAVAILABLE", code=review.result.code, **base)
        identifiers = []
        for instrument_id in started.result["instrument_ids"]:
            try:
                await assemble(
                    session,
                    base["article_id"],
                    instrument_id,
                    review.event_id,
                    cutoff=self.clock.utcnow(),
                    max_age=timedelta(hours=self.settings.news_max_age_hours),
                )
            except ValueError:
                continue
            receipt = await admit(
                session, base["article_id"], instrument_id, review.event_id, actor="news_scheduler"
            )
            identifiers.append(receipt.source_id)
        return ProductionResult(
            status="ADMITTED" if identifiers else "UNAVAILABLE",
            code="QUOTATIONS_ADMITTED" if identifiers else "NO_ADMISSIBLE_QUOTATIONS",
            admission_ids=tuple(identifiers),
            **base,
        )

    async def _finish(self, session, chain, result, started_id):
        records = await history(session, chain)
        state = job_state(records, result.group_event_id, self.clock.utcnow())
        if state.phase != "PENDING" or state.started.id != started_id:
            raise ValueError("Research completion superseded by control or newer attempt")
        await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=chain,
                event_type="NEWS_PRODUCTION_FINISHED",
                actor="news_scheduler",
                mode=TradingMode.PAPER,
            ),
            {"result": result.model_dump(mode="json")},
            expected_count=len(records),
        )
