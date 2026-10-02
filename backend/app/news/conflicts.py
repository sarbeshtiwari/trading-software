"""Audited lexical opposition warnings, never proof that unflagged reports agree."""

import re
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.modes import TradingMode
from app.news.dedupe import article_event, read_story
from app.news.entities import aware, history


class Statement(EvidenceModel):
    article_id: str
    article_audit_id: str
    publisher_id: str
    field: Literal["title", "body"]
    start: int
    end: int
    quote: str
    polarity: Literal["ASSERTED", "NEGATED"]


class Opposition(EvidenceModel):
    asserted: Statement
    negated: Statement


class ConflictAssessment(EvidenceModel):
    rule: Literal["LEXICAL_NEGATION_V1"] = "LEXICAL_NEGATION_V1"
    group_id: str
    group_event_id: str
    status: Literal["CONFLICTING", "UNASSESSED"]
    oppositions: list[Opposition]
    actionable: Literal[False] = False
    basis: Literal["HEURISTIC_WARNING_NOT_FACT_VERIFICATION"] = (
        "HEURISTIC_WARNING_NOT_FACT_VERIFICATION"
    )


class ConflictView(EvidenceModel):
    event_id: str
    known_at: AwareDatetime
    assessment: ConflictAssessment


def statements(text, *, member, field):
    result = []
    for sentence in re.finditer(r"[^.!?\n]+(?:[.!?]+|\n|$)", text):
        quote = sentence.group().strip()
        words = re.findall(r"\w+", quote.casefold())
        if (
            not 4 <= len(words) <= 200
            or len(quote) > 4000
            or any(
                word in words for word in ("if", "unless", "may", "might", "could", "reportedly")
            )
            or "not only" in quote.casefold()
        ):
            continue
        negations = sum(word in {"not", "never"} for word in words)
        if negations > 1:
            continue
        key = tuple(word for word in words if word not in {"not", "never"})
        start = sentence.start() + len(sentence.group()) - len(sentence.group().lstrip())
        result.append(
            (
                key,
                Statement(
                    article_id=member.article_id,
                    article_audit_id=member.article_audit_id,
                    publisher_id=member.publisher_id,
                    field=field,
                    start=start,
                    end=start + len(quote),
                    quote=quote,
                    polarity="NEGATED" if negations else "ASSERTED",
                ),
            )
        )
        if len(result) > 200:
            raise ValueError("Conflict statement scope exceeded")
    return result


def detect(statements_by_source):
    indexed = {}
    for key, statement in statements_by_source:
        indexed.setdefault(key, {"ASSERTED": [], "NEGATED": []})[statement.polarity].append(
            statement
        )
    oppositions = []
    for values in indexed.values():
        for positive in values["ASSERTED"]:
            for negative in values["NEGATED"]:
                if positive.article_id != negative.article_id:
                    oppositions.append(Opposition(asserted=positive, negated=negative))
                    if len(oppositions) > 100:
                        raise ValueError("Conflict pair scope exceeded")
    return oppositions


def conflict_chain(group_id):
    return "news-conflict-" + sha256(group_id.encode()).hexdigest()[:26]


async def assess_story(session, group_id, *, actor, clock=None):
    clock = clock or get_clock()
    now = clock.utcnow()
    story = await read_story(session, group_id, as_of=now)
    if story is None:
        raise ValueError("Story unavailable")
    extracted = []
    for member in story.story.members:
        event = await article_event(session, member, now)
        for field in ("title", "body"):
            extracted.extend(statements(event.result["article"][field], member=member, field=field))
    oppositions = detect(extracted)
    result = ConflictAssessment(
        group_id=group_id,
        group_event_id=story.event_id,
        status="CONFLICTING" if oppositions else "UNASSESSED",
        oppositions=oppositions,
    )
    chain = conflict_chain(group_id)
    records = await history(session, chain)
    if records and aware(records[-1].occurred_at) > now:
        raise ValueError("Conflict assessment clock regression")
    payload = result.model_dump(mode="json")
    if records and records[-1].result == payload:
        return records[-1]
    event = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=chain,
            event_type="NEWS_CONFLICT_ASSESSMENT",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": payload},
        expected_count=len(records),
    )
    if aware(event.occurred_at) < now:
        raise ValueError("Conflict assessment clock regression")
    return event


async def read_conflicts(session, story, *, as_of):
    records = await history(session, conflict_chain(story.group_id), as_of)
    if not records:
        return None
    event = records[-1]
    if event.event_type != "NEWS_CONFLICT_ASSESSMENT":
        raise ValueError("Conflict assessment integrity unavailable")
    assessment = ConflictAssessment.model_validate(event.result)
    if assessment.group_id != story.group_id or assessment.group_event_id != story.event_id:
        raise ValueError("Conflict assessment does not cover current story")
    return ConflictView(event_id=event.id, known_at=aware(event.occurred_at), assessment=assessment)
