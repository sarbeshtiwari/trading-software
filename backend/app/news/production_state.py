"""Replayable production-job state; owner actions never rewrite provider outcomes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.modes import TradingMode
from app.news.entities import aware


class ProductionResult(EvidenceModel):
    article_id: str
    group_event_id: str
    status: Literal["ADMITTED", "UNAVAILABLE", "DEGRADED", "RECOVERY_REQUIRED", "ABANDONED"]
    code: str
    admission_ids: tuple[str, ...] = ()
    interpretation_event_id: str | None = None


class ProductionView(EvidenceModel):
    article_id: str
    group_event_id: str
    event_id: str
    known_at: AwareDatetime
    state: Literal["PENDING", "FINISHED", "RETRY_AUTHORIZED", "RETRY_EXPIRED", "ABANDONED"]
    attempts: int
    retry_authorizations: int
    authorized_until: AwareDatetime | None
    result: ProductionResult | None
    billing_verification: Literal["UNVERIFIED"] = "UNVERIFIED"


@dataclass
class JobState:
    article_id: str
    group_event_id: str
    phase: str
    started: object
    head: object
    attempts: int = 1
    retry_authorizations: int = 0
    authorized_until: datetime | None = None
    result: ProductionResult | None = None

    def view(self, now):
        return ProductionView(
            article_id=self.article_id,
            group_event_id=self.group_event_id,
            event_id=self.head.id,
            known_at=aware(self.head.occurred_at),
            state="RETRY_EXPIRED" if self.expired(now) else self.phase,
            attempts=self.attempts,
            retry_authorizations=self.retry_authorizations,
            authorized_until=self.authorized_until,
            result=self.result,
        )

    def expired(self, now):
        return self.phase == "RETRY_AUTHORIZED" and self.authorized_until <= now


def job_state(records, group_event_id, now):
    if not records or records[0].event_type != "NEWS_PRODUCTION_STARTED":
        raise ValueError("Research job unavailable")
    first = records[0]
    state = JobState(first.result["article_id"], group_event_id, "PENDING", first, first)
    previous = aware(first.occurred_at)
    for index, event in enumerate(records):
        timestamp = aware(event.occurred_at)
        if event.mode != TradingMode.PAPER or not previous <= timestamp <= now:
            raise ValueError("Research job time or mode invalid")
        previous = timestamp
        if index and state.phase == "ABANDONED":
            raise ValueError("Abandoned research job was mutated")
        _transition(state, event, index)
        state.head = event
    return state


def _transition(state, event, index):
    kind, result = event.event_type, event.result
    if kind == "NEWS_PRODUCTION_STARTED":
        _start(state, event, index)
    elif kind == "NEWS_PRODUCTION_FINISHED":
        _complete(state, result)
    elif kind == "NEWS_PRODUCTION_RETRY_AUTHORIZED":
        if state.phase != "FINISHED" and not state.expired(aware(event.occurred_at)):
            raise ValueError("Research retry cannot replay a pending attempt")
        if state.result is None or state.result.status != "UNAVAILABLE":
            raise ValueError("Research retry requires a failed outcome")
        state.authorized_until = datetime.fromisoformat(result["authorized_until"])
        if state.authorized_until.utcoffset() is None or state.authorized_until <= aware(
            event.occurred_at
        ):
            raise ValueError("Research retry authorization time invalid")
        state.retry_authorizations += 1
        state.phase = "RETRY_AUTHORIZED"
    elif kind == "NEWS_PRODUCTION_RECOVERY_REQUESTED":
        if state.phase != "PENDING":
            raise ValueError("Only pending research can be recovered")
    elif kind == "NEWS_PRODUCTION_ABANDONED":
        state.phase = "ABANDONED"
    else:
        raise ValueError("Research job event invalid")


def _start(state, event, index):
    result = event.result
    if index and (state.phase != "RETRY_AUTHORIZED" or state.expired(aware(event.occurred_at))):
        raise ValueError("Research retry was not authorized")
    if result["article_id"] != state.article_id or result["group_event_id"] != state.group_event_id:
        raise ValueError("Research job identity mismatch")
    if index and result.get("authorization_id") != state.head.id:
        raise ValueError("Research retry authorization mismatch")
    state.started, state.phase, state.result = event, "PENDING", None
    state.attempts += bool(index)


def _complete(state, result):
    if state.phase != "PENDING":
        raise ValueError("Research completion without active attempt")
    parsed = ProductionResult.model_validate(result)
    if (parsed.article_id, parsed.group_event_id) != (state.article_id, state.group_event_id):
        raise ValueError("Research result identity mismatch")
    if parsed.status not in {"ADMITTED", "UNAVAILABLE"}:
        raise ValueError("Research completion is not definitive")
    state.result, state.phase = parsed, "FINISHED"
