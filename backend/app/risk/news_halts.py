"""Durable instrument entry inhibition; releases never erase acknowledged evidence."""

from hashlib import sha256

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError
from app.modes import TradingMode
from app.news.entities import aware, history
from app.notifications.outbox import enqueue


class NewsHaltError(SafetyError):
    default_code = "NEWS_HALT"


class NewsHaltState(EvidenceModel):
    instrument_id: str
    origin: DataOrigin
    blocked: bool = False
    event_id: str | None = None
    known_at: AwareDatetime | None = None
    causes: tuple[dict, ...] = ()
    acknowledged: tuple[str, ...] = ()


def chain_id(instrument_id, origin):
    return (
        "nh_"
        + sha256(f"PAPER:{DataOrigin(origin).value}:{instrument_id}".encode()).hexdigest()[:37]
    )


async def state_at(session, instrument_id, origin, *, as_of=None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff.utcoffset() is None or cutoff > get_clock().utcnow():
        raise ValueError("Aware nonfuture news halt cutoff required")
    records = await history(session, chain_id(instrument_id, origin), cutoff)
    state = NewsHaltState(instrument_id=instrument_id, origin=origin)
    for record in records:
        if (
            record.mode != TradingMode.PAPER
            or record.instrument_id != instrument_id
            or (state.known_at and aware(record.occurred_at) < state.known_at)
        ):
            raise ValueError("News halt identity or clock mismatch")
        if record.event_type == "NEWS_INSTRUMENT_HALT":
            cause = record.result["cause"]
            identifier = cause["admission_id"]
            if identifier in state.acknowledged or cause["origin"] != DataOrigin(origin).value:
                raise ValueError("News halt evidence identity mismatch")
            state = state.model_copy(
                update={
                    "blocked": True,
                    "causes": (*state.causes, cause),
                    "acknowledged": (*state.acknowledged, identifier),
                }
            )
        elif record.event_type == "NEWS_HALT_RELEASED":
            if record.result["expected_event_id"] != state.event_id or not state.blocked:
                raise ValueError("News halt release history mismatch")
            state = state.model_copy(update={"blocked": False, "causes": ()})
        else:
            raise ValueError("Unknown news halt event")
        state = state.model_copy(
            update={"event_id": record.id, "known_at": aware(record.occurred_at)}
        )
    return state


async def inhibit(session, instrument_id, origin, cause):
    state = await state_at(session, instrument_id, origin)
    if cause["admission_id"] in state.acknowledged:
        return state
    records = await history(session, chain_id(instrument_id, origin))
    if records and records[-1].id != state.event_id:
        raise ValueError("News halt clock regression")
    event = await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(instrument_id, origin),
            event_type="NEWS_INSTRUMENT_HALT",
            actor="news_reactions",
            mode=TradingMode.PAPER,
            severity="CRITICAL",
        ),
        {"instrument_id": instrument_id, "result": {"cause": cause}},
        expected_count=len(records),
    )
    await enqueue(
        session,
        key=f"news-halt:{event.id}",
        event_type="NEWS_INSTRUMENT_HALT",
        severity="CRITICAL",
        message=(
            f"NEWS HALT: entries blocked for {instrument_id}; audit={event.id}. "
            "Exits remain enabled."
        ),
        clock=get_clock(),
        source_event=event,
    )
    return await state_at(session, instrument_id, origin)


async def release(session, instrument_id, origin, *, expected_event_id, reason, actor):
    state = await state_at(session, instrument_id, origin)
    records = await history(session, chain_id(instrument_id, origin))
    if (
        not state.blocked
        or state.event_id != expected_event_id
        or records[-1].id != expected_event_id
        or len(reason.strip()) < 10
    ):
        raise ValueError("News halt changed or review reason unavailable")
    await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(instrument_id, origin),
            event_type="NEWS_HALT_RELEASED",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "instrument_id": instrument_id,
            "result": {
                "expected_event_id": expected_event_id,
                "reason": reason.strip(),
                "reviewed_causes": state.causes,
            },
        },
        expected_count=len(records),
    )
    return await state_at(session, instrument_id, origin)


async def require_no_news_halt(session, market):
    if market.mode != TradingMode.PAPER:
        return
    state = await state_at(session, market.instrument_id, market.data_origin)
    if state.blocked:
        raise NewsHaltError("NEWS_HALT", context={"event_id": state.event_id})
