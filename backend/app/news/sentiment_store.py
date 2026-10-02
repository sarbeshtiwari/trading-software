"""SENT-001: conservative reads from existing news storage.

Rows updated after the requested timestamp are excluded: the legacy news table
does not version interpretations. This avoids look-ahead but cannot reconstruct
an earlier overwritten interpretation. Returned evidence preserves what was used.
"""

from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import ValidationError

from app.core.clock import UTC
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.news import NewsItem
from app.fno.chain.model import aware
from app.news.sentiment import NewsEvidence, NewsSentiment, NewsSentimentPolicy, aggregate_news
from app.news.verification import verified_news


async def stored_news_sentiment(
    symbol: str,
    *,
    as_of: datetime,
    policy: NewsSentimentPolicy,
    origin: DataOrigin = DataOrigin.LIVE,
) -> NewsSentiment:
    cutoff = aware(as_of).astimezone(UTC)
    async with db_session.session_scope() as session:
        rows = (
            await session.scalars(
                sa.select(NewsItem).where(
                    NewsItem.created_at <= cutoff,
                    NewsItem.fetched_at <= cutoff,
                    sa.or_(NewsItem.updated_at.is_(None), NewsItem.updated_at <= cutoff),
                )
            )
        ).all()
        verified = {
            row.id: await verified_news(
                session,
                row,
                as_of=as_of,
                max_age=timedelta(seconds=policy.max_age_seconds),
                origin=origin,
            )
            for row in rows
        }
    evidence, excluded = [], {}
    for row in rows:
        if not isinstance(row.entities, list) or not any(
            isinstance(entity, dict) and entity.get("symbol") == symbol for entity in row.entities
        ):
            continue
        source = verified[row.id]
        if source is None:
            excluded[row.id] = "UNVERIFIED_SOURCE_OR_PROVENANCE"
            continue
        try:
            evidence.append(
                NewsEvidence(
                    id=row.id,
                    symbol=symbol,
                    url=row.url,
                    source_ids=tuple(sorted({item["publisher_id"] for item in source["sources"]})),
                    best_tier=source["best_tier"],
                    verification=row.verification_status,
                    published_at=_utc(row.published_at),
                    available_at=max(
                        *(
                            datetime.fromisoformat(item[field])
                            for item in source["sources"]
                            for field in ("configured_at", "fetched_at")
                        ),
                        *(
                            _utc(value)
                            for value in (
                                row.fetched_at,
                                row.created_at,
                                row.updated_at or row.created_at,
                            )
                        ),
                    ),
                    score=row.sentiment_score,
                    confidence=(row.interpretation or {}).get("confidence"),
                )
            )
        except (ValidationError, KeyError, TypeError, AttributeError):
            excluded[row.id] = "INVALID_OR_MISSING_EVIDENCE"
    result = aggregate_news(evidence, symbol=symbol, as_of=as_of, policy=policy)
    return result.model_copy(update={"excluded": {**excluded, **result.excluded}})


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
