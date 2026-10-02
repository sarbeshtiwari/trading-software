"""Durable source-policy admission of attributed quotations, not external fact checking."""

from datetime import timedelta
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.config import get_settings
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsSource
from app.modes import TradingMode
from app.news.conflicts import read_conflicts
from app.news.dedupe import article_story
from app.news.entities import aware, history, read_attribution
from app.news.ingest import read_article
from app.news.interpret import read_interpretation
from app.news.readiness import sources_available
from app.news.sources import SourcePolicy, chain_id, describe


class AdmissionView(EvidenceModel):
    source_id: str
    audit_id: str
    known_at: AwareDatetime
    status: Literal["SOURCE_POLICY_ADMITTED"] = "SOURCE_POLICY_ADMITTED"
    snapshot: dict


async def source_policy(session, member, cutoff):
    row = await session.scalar(sa.select(NewsSource).where(NewsSource.slug == member.source_slug))
    if row is None or (await describe(session, row)).integrity != "AUDITED":
        raise ValueError("Source configuration integrity unavailable")
    records = await history(session, chain_id(member.source_slug), cutoff)
    if not records or records[-1].id != member.source_event_id:
        raise ValueError("Source configuration changed")
    policy = SourcePolicy.model_validate(records[-1].result["configuration"])
    if not policy.is_enabled or policy.weight <= 0:
        raise ValueError("Source disabled or zero weight")
    return freeze_snapshot(
        {
            "slug": member.source_slug,
            "publisher_id": policy.publisher_id,
            "tier": policy.tier,
            "kind": policy.kind,
            "weight": policy.weight,
            "url": member.canonical_url,
            "fetched_at": member.observed_at,
            "configured_at": aware(records[-1].occurred_at),
            "source_event_id": member.source_event_id,
        }
    )


async def claim_support(session, member, claim, *, instrument_id, cutoff, max_age):
    observation = await read_article(session, member.article_id, as_of=cutoff)
    if observation is None or not cutoff - max_age <= observation.article.published_at <= cutoff:
        return None
    attribution = await read_attribution(
        session, member.article_id, member.article_audit_id, as_of=cutoff
    )
    if attribution is None:
        return None
    offset = 0 if claim.field == "title" else len(observation.article.title) + 1
    if not any(
        instrument_id in mention.instrument_ids
        and claim.start + offset <= mention.start < mention.end <= claim.end + offset
        for mention in attribution.attribution.matches
    ):
        return None
    text = getattr(observation.article, claim.field)
    if not claim.start < claim.end <= len(text) or text[claim.start : claim.end] != claim.quote:
        raise ValueError("Claim anchor unavailable")
    source = await source_policy(session, member, cutoff)
    return {
        "claim": claim.model_dump(mode="json"),
        "source": source,
        "article_audit_id": member.article_audit_id,
        "entity_event_id": attribution.event_id,
        "body_fingerprint": sha256(
            " ".join(observation.article.body.casefold().split()).encode()
        ).hexdigest(),
        "published_at": observation.article.published_at.isoformat(),
        "data_origin": observation.article.data_origin.value,
    }


async def assemble(session, article_id, instrument_id, interpretation_id, *, cutoff, max_age):
    settings = get_settings()
    if not settings.news_enabled or max_age <= timedelta(0):
        raise ValueError("News research disabled or stale")
    review = await read_interpretation(session, article_id, as_of=cutoff)
    if (
        review is None
        or review.event_id != interpretation_id
        or not review.current_group_matches
        or review.result.status != "GROUNDED_UNVERIFIED"
        or review.result.result is None
        or review.result.result.dropped
        or review.result.result.interpretation is None
    ):
        raise ValueError("Grounded interpretation unavailable or changed")
    story = await article_story(session, article_id, as_of=cutoff)
    conflict = await read_conflicts(session, story, as_of=cutoff) if story else None
    if (
        conflict is None
        or conflict.assessment.status == "CONFLICTING"
        or story.story.ambiguous_group_match
    ):
        raise ValueError("Conflict assessment unavailable or blocking")
    members = {member.article_id: member for member in story.story.members}
    grouped = {}
    for claim in review.result.result.interpretation.claims:
        support = await claim_support(
            session,
            members[claim.article_id],
            claim,
            instrument_id=instrument_id,
            cutoff=cutoff,
            max_age=max_age,
        )
        if support is not None:
            grouped.setdefault(" ".join(claim.quote.casefold().split()), []).append(support)
    admitted = []
    for supports in grouped.values():
        selected = corroborating_support(supports)
        if selected:
            if await sources_available(
                session, [support["source"] for support in selected], cutoff, settings
            ):
                admitted.append(selected)
    if not admitted:
        raise ValueError("No independently supported attributed quotation")
    sources = {
        support["source"]["slug"]: support["source"] for group in admitted for support in group
    }
    published = [support["published_at"] for group in admitted for support in group]
    return freeze_snapshot(
        {
            "basis": "SOURCE_POLICY_ADMITTED_QUOTATIONS_NOT_EXTERNAL_FACT_CHECK",
            "group_id": story.group_id,
            "group_event_id": story.event_id,
            "interpretation_event_id": review.event_id,
            "conflict_event_id": conflict.event_id,
            "article_id": article_id,
            "instrument_id": instrument_id,
            "data_origin": story.story.data_origin.value,
            "title": "Source-policy admitted quotations",
            "body": "\n".join(group[0]["claim"]["quote"] for group in admitted),
            "claims": admitted,
            "sources": list(sources.values()),
            "published_at": min(published),
            "best_tier": min(source["tier"] for source in sources.values()),
            "publisher_count": len({source["publisher_id"] for source in sources.values()}),
            "entities": [{"instrument_id": instrument_id}],
            "verification": "SOURCE_POLICY_ADMITTED",
            "sentiment_score": None,
            "interpretation": None,
            "standalone_trigger_allowed": False,
        }
    )


def corroborating_support(supports):
    primary = [support for support in supports if support["source"]["tier"] == 1]
    if primary:
        return primary
    for index, first in enumerate(supports):
        for second in supports[index + 1 :]:
            if (
                first["source"]["publisher_id"] != second["source"]["publisher_id"]
                and urlsplit(first["source"]["url"]).hostname
                != urlsplit(second["source"]["url"]).hostname
                and first["body_fingerprint"] != second["body_fingerprint"]
            ):
                return [first, second]
    return []


async def record_admission(session, article_id, instrument_id, interpretation_id, *, actor):
    now = get_clock().utcnow()
    settings = get_settings()
    if settings.trading_mode != TradingMode.PAPER:
        raise ValueError("Research admission is PAPER-only")
    snapshot = await assemble(
        session,
        article_id,
        instrument_id,
        interpretation_id,
        cutoff=now,
        max_age=timedelta(hours=settings.news_max_age_hours),
    )
    identifier = (
        "nra_"
        + sha256((article_id + ":" + instrument_id + ":" + interpretation_id).encode()).hexdigest()[
            :32
        ]
    )
    records = await history(session, identifier)
    if records:
        if (
            len(records) != 1
            or records[0].event_type != "NEWS_RESEARCH_ADMITTED"
            or records[0].instrument_id != instrument_id
            or records[0].mode != TradingMode.PAPER
            or records[0].result != snapshot
            or aware(records[0].occurred_at) > now
        ):
            raise ValueError("Research admission identity mismatch")
        event = records[0]
    else:
        event = await AuditService().append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier,
                event_type="NEWS_RESEARCH_ADMITTED",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {"result": snapshot, "instrument_id": instrument_id},
            expected_count=0,
        )
        if aware(event.occurred_at) < now:
            raise ValueError("Research admission clock regression")
    return AdmissionView(
        source_id=identifier,
        audit_id=event.id,
        known_at=aware(event.occurred_at),
        snapshot=snapshot,
    )


async def admitted_news(session, identifier, *, instrument_id, as_of, max_age, origin):
    try:
        if as_of.utcoffset() is None or as_of > get_clock().utcnow():
            return None
        cutoff = as_of.astimezone(UTC)
        records = await history(session, identifier, cutoff)
        if (
            len(records) != 1
            or records[0].event_type != "NEWS_RESEARCH_ADMITTED"
            or records[0].mode != TradingMode.PAPER
            or records[0].instrument_id != instrument_id
        ):
            return None
        event = records[0]
        snapshot = event.result
        if snapshot["data_origin"] != DataOrigin(origin).value:
            return None
        current = await assemble(
            session,
            snapshot["article_id"],
            instrument_id,
            snapshot["interpretation_event_id"],
            cutoff=cutoff,
            max_age=min(max_age, timedelta(hours=get_settings().news_max_age_hours)),
        )
        if current != snapshot:
            return None
        return {
            **snapshot,
            "source_id": identifier,
            "known_at": aware(event.occurred_at).isoformat(),
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


async def admitted_for_instrument(session, instrument_id, *, as_of, max_age, origin):
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.event_type == "NEWS_RESEARCH_ADMITTED",
                    AuditEvent.instrument_id == instrument_id,
                    AuditEvent.occurred_at <= as_of.astimezone(UTC),
                    AuditEvent.occurred_at >= as_of.astimezone(UTC) - max_age,
                )
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                .limit(1001)
            )
        ).all()
    )
    if len(events) > 1000:
        raise ValueError("Research admission scope exceeded")
    selected, groups = [], set()
    for event in events:
        snapshot = await admitted_news(
            session,
            event.chain_id,
            instrument_id=instrument_id,
            as_of=as_of,
            max_age=max_age,
            origin=origin,
        )
        if snapshot is not None and snapshot["group_id"] not in groups:
            groups.add(snapshot["group_id"])
            selected.append(snapshot)
    return selected
