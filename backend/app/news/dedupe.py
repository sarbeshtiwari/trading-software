"""Conservative story grouping; text similarity is never factual corroboration."""

import re
from datetime import timedelta
from hashlib import sha256
from itertools import pairwise
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db.models.audit import AuditEvent
from app.modes import TradingMode
from app.news.entities import aware, history


class StoryMember(EvidenceModel):
    article_id: str
    article_audit_id: str
    source_slug: str
    source_event_id: str
    publisher_id: str
    canonical_url: str
    title: str
    published_at: AwareDatetime
    observed_at: AwareDatetime


class Story(EvidenceModel):
    version: Literal[1] = 1
    algorithm: Literal["COMPLETE_LINK_TEXT_V1"] = "COMPLETE_LINK_TEXT_V1"
    verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    data_origin: DataOrigin
    members: list[StoryMember]
    ambiguous_group_match: bool = False


class StoryView(EvidenceModel):
    group_id: str
    event_id: str
    known_at: AwareDatetime
    story: Story


def canonical_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid article URL")
    authority = parsed.hostname.lower()
    if parsed.port not in (None, 443):
        authority += f":{parsed.port}"
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))


def tokens(value):
    return re.findall(r"\w+", value.casefold())


def similarity(first, second):
    left, right = set(first), set(second)
    return len(left & right) / len(left | right) if left and right else 0


def same_story(first, second):
    title_left, title_right = tokens(first["title"]), tokens(second["title"])
    body_left, body_right = tokens(first["body"]), tokens(second["body"])
    if not body_left or not body_right:
        return False
    if (
        first.get("url")
        and second.get("url")
        and canonical_url(first["url"]) == canonical_url(second["url"])
        and similarity(title_left, title_right) >= 0.8
    ):
        return True
    if body_left == body_right and similarity(title_left, title_right) >= 0.8:
        return True
    if min(len(body_left), len(body_right)) < 20:
        return False
    left_pairs = list(pairwise(body_left))
    right_pairs = list(pairwise(body_right))
    return similarity(title_left, title_right) >= 0.8 and similarity(left_pairs, right_pairs) >= 0.9


def member_for(event):
    sealed = event.result
    article = sealed["article"]
    if not event.correlation_id:
        raise ValueError("Article grouping linkage unavailable")
    return StoryMember(
        article_id=event.correlation_id,
        article_audit_id=event.id,
        source_slug=sealed["source_slug"],
        source_event_id=sealed["source_event_id"],
        publisher_id=sealed["source_policy"]["publisher_id"].strip().casefold(),
        canonical_url=canonical_url(article["url"]),
        title=article["title"],
        published_at=article["published_at"],
        observed_at=aware(event.occurred_at),
    )


async def article_event(session, member, cutoff):
    chain = "article-" + sha256(member.article_id.encode()).hexdigest()[:32]
    records = await history(session, chain, cutoff)
    if (
        len(records) != 1
        or records[0].id != member.article_audit_id
        or records[0].event_type != "NEWS_ARTICLE_IMPORTED"
        or member_for(records[0]) != member
    ):
        raise ValueError("Story member evidence unavailable")
    return records[0]


async def candidate_groups(session, now):
    candidates = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.event_type == "NEWS_STORY_GROUP",
                    AuditEvent.occurred_at >= now - timedelta(hours=24),
                )
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.sequence.desc())
                .limit(1001)
            )
        ).all()
    )
    if len(candidates) > 1000:
        raise ValueError("Story grouping scope exceeded")
    latest = {}
    for candidate in candidates:
        if aware(candidate.occurred_at) > now:
            raise ValueError("Story grouping clock regression")
        latest.setdefault(candidate.chain_id, candidate)
    groups = []
    for candidate in latest.values():
        records = await history(session, candidate.chain_id)
        if not records or records[-1].id != candidate.id:
            raise ValueError("Story history changed")
        story = Story.model_validate(candidate.result)
        groups.append((candidate, records, story))
    return groups


async def group_story(session, event, *, actor, clock=None):
    clock = clock or get_clock()
    now = clock.utcnow()
    cursor = await history(session, "news-grouping-cursor")
    member = member_for(event)
    if member.observed_at > now or (cursor and aware(cursor[-1].occurred_at) > now):
        raise ValueError("Story grouping clock regression")
    matches = []
    for candidate, records, story in await candidate_groups(session, now):
        if any(stored.article_id == member.article_id for stored in story.members):
            return candidate
        if story.data_origin.value != event.result["article"]["data_origin"]:
            continue
        if any(
            abs(stored.published_at - member.published_at) > timedelta(hours=24)
            for stored in story.members
        ):
            continue
        compatible = True
        for stored in story.members:
            previous = await article_event(session, stored, now)
            if not same_story(event.result["article"], previous.result["article"]):
                compatible = False
                break
        if compatible:
            matches.append((candidate, records, story))
    if len(matches) == 1:
        candidate, records, story = matches[0]
        if len(story.members) >= 20:
            raise ValueError("Story source scope exceeded")
        chain = candidate.chain_id
        story = story.model_copy(update={"members": [*story.members, member]})
        expected_count = len(records)
    else:
        chain = "news-story-" + sha256(member.article_id.encode()).hexdigest()[:29]
        story = Story(
            data_origin=event.result["article"]["data_origin"],
            members=[member],
            ambiguous_group_match=len(matches) > 1,
        )
        expected_count = 0
    result = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=chain, event_type="NEWS_STORY_GROUP", actor=actor, mode=TradingMode.PAPER
        ),
        {"result": story.model_dump(mode="json")},
        expected_count=expected_count,
    )
    if aware(result.occurred_at) < now:
        raise ValueError("Story grouping clock regression")
    await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=membership_chain(member.article_id),
            event_type="NEWS_STORY_MEMBERSHIP",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": {"group_id": chain, "article_id": member.article_id}},
        expected_count=0,
    )
    await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id="news-grouping-cursor",
            event_type="NEWS_GROUPING_CURSOR",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": {"article_id": member.article_id, "group_id": chain}},
        expected_count=len(cursor),
    )
    return result


def membership_chain(article_id):
    return "news-member-" + sha256(article_id.encode()).hexdigest()[:28]


async def article_story(session, article_id, *, as_of):
    records = await history(session, membership_chain(article_id), as_of)
    if not records:
        return None
    if (
        len(records) != 1
        or records[0].event_type != "NEWS_STORY_MEMBERSHIP"
        or records[0].result["article_id"] != article_id
    ):
        raise ValueError("Story membership unavailable")
    result = await read_story(session, records[0].result["group_id"], as_of=as_of)
    if result is None or not any(
        member.article_id == article_id for member in result.story.members
    ):
        raise ValueError("Story membership mismatch")
    return result


async def read_story(session, group_id, *, as_of):
    if as_of.utcoffset() is None:
        raise ValueError("Aware story cutoff required")
    cutoff = as_of.astimezone(UTC)
    records = await history(session, group_id, cutoff)
    if not records:
        return None
    event = records[-1]
    if event.event_type != "NEWS_STORY_GROUP" or not verify_records(records):
        raise ValueError("Story audit unavailable")
    story = Story.model_validate(event.result)
    for member in story.members:
        await article_event(session, member, cutoff)
    return StoryView(
        group_id=group_id, event_id=event.id, known_at=aware(event.occurred_at), story=story
    )
