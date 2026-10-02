"""Opt-in application scheduler; acquisition never certifies actionable news."""

import asyncio
from datetime import timedelta

import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.core.clock import get_clock
from app.core.enums import HealthStatus
from app.db import session as db_session
from app.db.models.news import NewsSource
from app.modes import TradingMode
from app.monitoring.healthchecks import HealthCheck
from app.news.ingest import utc
from app.news.polling import NewsPoller, acquisition_state, poll_history
from app.news.production import ResearchProducer
from app.news.sources import describe


class NewsRuntime:
    def __init__(self, settings=None, *, poller=None, clock=None):
        self.settings = settings or get_settings()
        self.clock = clock or get_clock()
        self.poller = poller or NewsPoller(clock=self.clock)
        self.research = ResearchProducer(self.settings, clock=self.clock)
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.lock = asyncio.Lock()
        self.running = False
        self.cursor = ""
        self.last_cycle = None
        self.status = "DISABLED"
        self.processed = 0
        self.task = None

    async def start(self):
        if self.running:
            raise ValueError("News scheduler already running")
        if not self.settings.news_polling_enabled or not self.settings.news_enabled:
            return
        if (
            self.settings.trading_mode != TradingMode.PAPER
            or self.settings.news_poll_interval_seconds < 30
        ):
            self.status = "UNAVAILABLE"
            return
        self.running = True
        self.status = "STARTING"
        try:
            self.scheduler.add_job(
                self.cycle,
                "interval",
                seconds=self.settings.news_poll_interval_seconds,
                id="news-acquisition",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,
                next_run_time=self.clock.utcnow(),
            )
            self.scheduler.start()
        except Exception:
            self.running = False
            self.status = "UNAVAILABLE"
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)

    async def cycle(self):
        if not self.running or self.lock.locked():
            return
        async with self.lock:
            self.task = asyncio.current_task()
            failed = False
            self.processed = 0
            try:
                async with db_session.session_scope() as session:
                    rows = list(
                        (
                            await session.scalars(
                                sa.select(NewsSource)
                                .where(
                                    NewsSource.is_enabled.is_(True), NewsSource.slug > self.cursor
                                )
                                .order_by(NewsSource.slug)
                                .limit(self.settings.news_poll_batch_size)
                            )
                        ).all()
                    )
                    candidates = []
                    for row in rows:
                        self.cursor = row.slug
                        view = await describe(session, row)
                        if view.integrity != "AUDITED" or view.configuration.kind not in {
                            "rss",
                            "api",
                        }:
                            failed = True
                            continue
                        history = await poll_history(session, row.slug)
                        if history and self.clock.utcnow() < utc(
                            history[-1].occurred_at
                        ) + timedelta(seconds=self.settings.news_poll_interval_seconds):
                            state = await acquisition_state(session, row.slug)
                            failed = failed or state.status not in {"ACQUIRED_UNVERIFIED", "EMPTY"}
                            continue
                        candidates.append((row.slug, view.event_id))
                if len(rows) < self.settings.news_poll_batch_size:
                    self.cursor = ""
                for slug, version in candidates:
                    try:
                        result = await self.poller.poll(
                            slug, expected_event_id=version, actor="news_scheduler"
                        )
                        self.processed += 1
                        failed = failed or result.status == "DEGRADED"
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed = True
                await self.research.cycle()
                failed = failed or self.research.status in {"DEGRADED", "UNAVAILABLE"}
                self.status = "DEGRADED" if failed else "IDLE" if not rows else "RUNNING"
                self.last_cycle = self.clock.utcnow()
            except asyncio.CancelledError:
                self.status = "STOPPED"
                raise
            except Exception:
                self.status = "DEGRADED"
                if self.settings.news_research_enabled:
                    self.research.status = "DEGRADED"
                self.last_cycle = self.clock.utcnow()
            finally:
                self.task = None

    async def stop(self):
        self.running = False
        if self.scheduler.running:
            self.scheduler.pause()
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.status = "STOPPED"
        if self.settings.news_research_enabled:
            self.research.status = "STOPPED"

    def snapshot(self):
        status = self.status
        if self.running and self.last_cycle is not None:
            elapsed = self.clock.utcnow() - self.last_cycle
            if elapsed < timedelta(0) or elapsed > timedelta(
                seconds=self.settings.news_poll_interval_seconds * 2
                + self.settings.news_poll_batch_size * 20
                + self.settings.news_research_batch_size
                * (self.settings.llm_max_repair_attempts + 1)
                * (min(self.settings.llm_timeout_seconds, self.settings.paper_cycle_seconds) + 1)
            ):
                status = "STALE"
        return {
            "status": status,
            "running": self.running,
            "last_cycle": self.last_cycle,
            "processed_last_cycle": self.processed,
            "automatic_enabled": self.settings.news_polling_enabled,
            "actionable_news": "PER_INSTRUMENT_CHECK_REQUIRED"
            if self.settings.news_research_enabled and self.last_cycle is not None
            else "UNAVAILABLE",
            "research_enabled": self.settings.news_research_enabled,
            "research_status": self.research.status,
            "research_results": [
                result.model_dump(mode="json") for result in self.research.results
            ],
        }


class NewsAcquisitionCheck(HealthCheck):
    name = "news_acquisition"
    critical = False

    def __init__(self, runtime):
        self.runtime = runtime

    async def run(self):
        snapshot = self.runtime.snapshot()
        status = HealthStatus.PASS if snapshot["status"] == "RUNNING" else HealthStatus.DEGRADED
        return (
            status,
            "Current acquisition batch only; research usability requires instrument-level checks",
            snapshot,
        )
