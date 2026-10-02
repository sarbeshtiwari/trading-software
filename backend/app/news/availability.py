"""Point-in-time research admission, independent of caller-supplied availability flags."""

from datetime import timedelta
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.equity import EvidenceModel
from app.config import get_settings
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.news.research import admitted_for_instrument
from app.news.verification import verified_news


class NewsResearch(EvidenceModel):
    status: Literal["AVAILABLE", "UNAVAILABLE", "DEGRADED"]
    code: str
    as_of: AwareDatetime
    data_origin: DataOrigin
    articles: tuple[dict, ...] = ()


async def research_at(instrument_id, *, as_of, origin, max_age_seconds):
    if as_of.utcoffset() is None:
        raise ValueError("Aware research timestamp required")
    as_of = as_of.astimezone(UTC)
    settings = get_settings()
    base = {"as_of": as_of, "data_origin": origin}
    if (
        as_of.utcoffset() is None
        or as_of > get_clock().now()
        or max_age_seconds <= 0
        or settings.news_max_age_hours <= 0
    ):
        return NewsResearch(status="UNAVAILABLE", code="NEWS_TIME_UNAVAILABLE", **base)
    if not settings.news_enabled:
        return NewsResearch(status="DEGRADED", code="NEWS_SERVICE_DISABLED", **base)
    max_age = timedelta(seconds=min(max_age_seconds, settings.news_max_age_hours * 3600))
    try:
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(NewsItem)
                        .where(
                            NewsItem.published_at >= as_of - max_age,
                            NewsItem.published_at <= as_of,
                            NewsItem.created_at <= as_of,
                            NewsItem.fetched_at <= as_of,
                            sa.or_(NewsItem.updated_at.is_(None), NewsItem.updated_at <= as_of),
                            NewsItem.data_origin == DataOrigin(origin).value,
                        )
                        .order_by(NewsItem.published_at.desc(), NewsItem.id)
                        .limit(1001)
                    )
                ).all()
            )
            if len(rows) > 1000:
                return NewsResearch(status="UNAVAILABLE", code="NEWS_SCOPE_EXCEEDED", **base)
            articles = await admitted_for_instrument(
                session, instrument_id, as_of=as_of, max_age=max_age, origin=origin
            )
            for row in rows:
                if not isinstance(row.entities, list) or not any(
                    isinstance(entity, dict) and entity.get("instrument_id") == instrument_id
                    for entity in row.entities
                ):
                    continue
                snapshot = await verified_news(
                    session, row, as_of=as_of, max_age=max_age, origin=origin
                )
                if snapshot is None:
                    continue
                articles.append(snapshot)
        return NewsResearch(
            status="AVAILABLE" if articles else "UNAVAILABLE",
            code="NEWS_AVAILABLE" if articles else "NEWS_RESEARCH_UNAVAILABLE",
            articles=tuple(articles),
            **base,
        )
    except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError):
        return NewsResearch(status="DEGRADED", code="NEWS_SERVICE_DEGRADED", **base)
