"""Shared point-in-time source checks; a stored VERIFIED flag is not corroboration."""

from datetime import datetime, timedelta
from urllib.parse import urlsplit

import sqlalchemy as sa

from app.audit.snapshots import freeze_snapshot
from app.config import get_settings
from app.core.clock import UTC
from app.core.data_origin import DataOrigin
from app.core.enums import VerificationStatus
from app.db.models.news import NewsSource
from app.news.readiness import sources_available


def _utc(value):
    if not isinstance(value, datetime):
        raise ValueError("news timestamp unavailable")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _host(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL unavailable or unsafe")
    return parsed.hostname.lower()


def _source_snapshot(source, attribution, *, published, cutoff):
    fetched = datetime.fromisoformat(attribution["fetched_at"].replace("Z", "+00:00"))
    if (
        fetched.utcoffset() is None
        or not published <= fetched <= cutoff
        or not source.is_enabled
        or not source.publisher_id
        or not source.publisher_id.strip()
        or not 1 <= source.tier <= 4
        or (source.tier == 1 and source.kind not in {"filings", "regulator"})
        or _utc(source.created_at) > cutoff
        or _utc(source.updated_at or source.created_at) > cutoff
        or _host(source.endpoint) != _host(attribution["url"])
    ):
        raise ValueError("source is not point-in-time admissible")
    return freeze_snapshot(
        {
            "slug": source.slug,
            "publisher_id": source.publisher_id.strip().casefold(),
            "tier": source.tier,
            "kind": source.kind,
            "url": attribution["url"],
            "fetched_at": fetched,
            "configured_at": _utc(source.updated_at or source.created_at),
        }
    )


async def verified_news(session, row, *, as_of, max_age, origin):
    try:
        settings = get_settings()
        max_age = min(max_age, timedelta(hours=settings.news_max_age_hours))
        cutoff = _utc(as_of)
        published = _utc(row.published_at)
        if (
            as_of.utcoffset() is None
            or not settings.news_enabled
            or max_age <= timedelta(0)
            or row.verification_status != VerificationStatus.VERIFIED
            or row.conflicts_with
            or not row.body
            or not row.body.strip()
            or row.data_origin != DataOrigin(origin).value
            or not cutoff - max_age <= published <= _utc(row.fetched_at) <= cutoff
            or _utc(row.created_at) > cutoff
            or _utc(row.updated_at or row.created_at) > cutoff
            or not isinstance(row.sources, list)
            or not 1 <= len(row.sources) <= 20
        ):
            return None
        slugs = [source["slug"] for source in row.sources]
        if len(set(slugs)) != len(slugs) or any(not isinstance(slug, str) for slug in slugs):
            return None
        configured = {
            source.slug: source
            for source in (
                await session.scalars(sa.select(NewsSource).where(NewsSource.slug.in_(slugs)))
            ).all()
        }
        sources = tuple(
            _source_snapshot(
                configured[source["slug"]],
                source,
                published=published,
                cutoff=cutoff,
            )
            for source in row.sources
        )
        publishers = {source["publisher_id"] for source in sources}
        best_tier = min(source["tier"] for source in sources)
        if best_tier != 1 and len(publishers) < 2:
            return None
        if not await sources_available(session, sources, as_of, settings):
            return None
        return freeze_snapshot(
            {
                "source_id": row.id,
                "published_at": published,
                "fetched_at": _utc(row.fetched_at),
                "updated_at": _utc(row.updated_at or row.created_at),
                "title": row.title,
                "body": row.body,
                "sources": sources,
                "entities": row.entities,
                "verification": row.verification_status.value,
                "data_origin": row.data_origin,
                "best_tier": best_tier,
                "publisher_count": len(publishers),
                "interpretation": row.interpretation,
                "sentiment_score": row.sentiment_score,
            }
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
