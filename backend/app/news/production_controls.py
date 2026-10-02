"""Authenticated callers may recover receipts, abandon work or authorize safe retries."""

from datetime import timedelta
from typing import Literal

from pydantic import Field, field_validator

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.modes import TradingMode
from app.news.entities import history
from app.news.interpret import read_interpretation
from app.news.production import ResearchProducer, production_chain
from app.news.production_state import job_state

NO_CALL_FAILURES = frozenset(
    {
        "PROVIDER_UNAVAILABLE",
        "CREDENTIALS_UNAVAILABLE",
        "TARIFF_OR_MODEL_UNAVAILABLE",
        "BUDGET_STORAGE_UNSUPPORTED",
        "CIRCUIT_OPEN_OR_BUSY",
        "BUDGET_EXCEEDED",
    }
)


class ProductionControl(EvidenceModel):
    action: Literal["RECOVER", "ABANDON", "AUTHORIZE_RETRY"]
    expected_event_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    authorization_seconds: int = Field(default=300, ge=1, le=900, strict=True)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 10:
            raise ValueError("Meaningful recovery reason required")
        return value.strip()


async def read_job(session, group_event_id):
    records = await history(session, production_chain(group_event_id))
    return job_state(records, group_event_id, get_clock().utcnow()).view(get_clock().utcnow())


async def control_job(session, group_event_id, control, *, actor):
    settings, now = get_settings(), get_clock().utcnow()
    if settings.trading_mode != TradingMode.PAPER:
        raise ValueError("Research controls are PAPER-only")
    chain = production_chain(group_event_id)
    records = await history(session, chain)
    state = job_state(records, group_event_id, now)
    if state.head.id != control.expected_event_id or state.phase == "ABANDONED":
        raise ValueError("Research job changed or abandoned")
    result = {"reason": control.reason, "previous_event_id": state.head.id}
    if control.action == "AUTHORIZE_RETRY":
        await _require_retry(session, state, settings, now)
        result["authorized_until"] = (
            now + timedelta(seconds=control.authorization_seconds)
        ).isoformat()
        result["maximum_owner_retries"] = settings.news_research_max_owner_retries
        kind = "NEWS_PRODUCTION_RETRY_AUTHORIZED"
    elif control.action == "RECOVER":
        if state.phase != "PENDING":
            raise ValueError("Only a pending job can recover a receipt")
        kind = "NEWS_PRODUCTION_RECOVERY_REQUESTED"
    else:
        kind = "NEWS_PRODUCTION_ABANDONED"
    await AuditService().append_in_session(
        session,
        AuditIdentity(chain_id=chain, event_type=kind, actor=actor, mode=TradingMode.PAPER),
        {"result": result},
        expected_count=len(records),
    )
    if control.action == "RECOVER":
        records = await history(session, chain)
        await ResearchProducer(settings)._resume(session, records, group_event_id)
    return await read_job(session, group_event_id)


async def _require_retry(session, state, settings, now):
    if state.phase != "FINISHED" and not state.expired(now):
        raise ValueError("Retry cannot repeat a pending or ambiguous call")
    result = state.result
    if (
        state.retry_authorizations >= settings.news_research_max_owner_retries
        or result is None
        or result.status != "UNAVAILABLE"
        or result.code not in NO_CALL_FAILURES
        or result.interpretation_event_id is None
    ):
        raise ValueError("Research retry limit or eligibility unavailable")
    review = await read_interpretation(session, state.article_id, as_of=now)
    if (
        review is None
        or not review.current_group_matches
        or review.event_id != result.interpretation_event_id
        or review.result.call_id is not None
        or review.result.code != result.code
        or review.result.group_event_id != state.group_event_id
    ):
        raise ValueError("No-call interpretation receipt unavailable")
