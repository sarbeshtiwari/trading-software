"""Optional grounded review ahead of the unchanged deterministic decision gates."""

import asyncio
from hashlib import sha256
from time import perf_counter

import sqlalchemy as sa

from app.agents.tasks import PROPOSAL_AGENT, REGISTRY, validate_proposal_agent
from app.audit.service import AuditIdentity, AuditService
from app.core.data_origin import DataOrigin
from app.core.ids import new_id
from app.core.logging import redact_data
from app.db import session as db_session
from app.db.models.llm import LLMCall
from app.llm import budget
from app.llm.base import LLMSchemaError, ProviderFailure
from app.llm.claude import ClaudeProvider
from app.llm.factory import provider_for
from app.llm.fallback import DeterministicFallbackProvider
from app.llm.receipts import ReceiptProposal
from app.llm.schema import decode_json, parse_review
from app.llm.telemetry import record_fallback
from app.modes import TradingMode
from app.notifications.outbox import enqueue
from app.risk.event_controls import state_at as event_state


async def finish(
    identifier,
    settings,
    *,
    clock,
    mode,
    reply,
    review,
    outcome,
    latency_ms,
    event_type="ADVISORY_PROPOSAL",
):
    async with db_session.session_scope() as session:
        call = await session.get(LLMCall, identifier)
        if call is None:
            raise RuntimeError("LLM reservation missing")
        claimed = await session.execute(
            sa.update(LLMCall)
            .where(LLMCall.id == identifier, LLMCall.outcome == "PENDING")
            .values(outcome=outcome)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise RuntimeError("LLM reservation already settled")
        await budget.settle(session, call, settings, clock=clock, reply=reply, outcome=outcome)
        call.outcome = outcome
        call.latency_ms = latency_ms
        call.raw_response = redact_data(reply.raw) if reply is not None else None
        call.parsed_response = redact_data(review.model_dump(mode="json")) if review else None
        call.schema_valid = review is not None
        call.error_detail = outcome if outcome != "SUCCESS" else None
        event = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier,
                event_type=event_type,
                actor="claude_advisory",
                mode=mode,
            ),
            {
                "correlation_id": call.correlation_id,
                "result": {
                    "outcome": outcome,
                    "review": call.parsed_response,
                    "cost_usd": call.cost_usd,
                    "cost_basis": call.request_context["tariff"]["basis"],
                    "reservation_retained": call.cost_usd is None,
                    "raw_response_hash": sha256(call.raw_response.encode()).hexdigest()
                    if call.raw_response is not None
                    else None,
                },
            },
        )
        if outcome != "SUCCESS":
            await _notify(session, event, clock, outcome)


async def _notify(session, event, clock, code):
    await enqueue(
        session,
        key=event.id,
        event_type="LLM_DEGRADED",
        severity="WARNING",
        message="LLM advisory degraded: " + code + ". Deterministic gates remain mandatory.",
        clock=clock,
        source_event=event,
    )


async def denied(code, *, clock, mode, correlation_id):
    async with db_session.session_scope() as session:
        event = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=new_id("llm"),
                event_type="ADVISORY_UNAVAILABLE",
                actor="llm_service",
                mode=mode,
            ),
            {"correlation_id": correlation_id, "result": {"reason": code}},
        )
        await _notify(session, event, clock, code)


async def review_signal(
    inputs, settings, *, clock, mode, correlation_id, task=None, decision_binding=None
):
    inputs = inputs.model_copy(update={"event_control": None})
    if task is not None:
        task = _registered_task(task, decision_binding)
    if inputs.signal.data_origin in (DataOrigin.HISTORICAL, DataOrigin.REPLAY):
        return await record_fallback(
            inputs,
            clock=clock,
            mode=mode,
            correlation_id=correlation_id,
            reason="HISTORICAL_LLM_REVIEW_DISABLED_NO_LOOKAHEAD",
        )
    expected = {f"{item.kind}:{item.source_id}" for item in inputs.evidence}
    if not expected or set(inputs.grounded_evidence) != expected:
        await denied("GROUNDING_UNAVAILABLE", clock=clock, mode=mode, correlation_id=correlation_id)
        return await record_fallback(
            inputs,
            clock=clock,
            mode=mode,
            correlation_id=correlation_id,
            reason="GROUNDING_UNAVAILABLE",
        )
    inputs = await _calendar_context(inputs, mode)
    provider = provider_for(settings)
    reason = "REMOTE_PROVIDER_UNAVAILABLE"
    if isinstance(provider, ClaudeProvider):
        for attempt in range(settings.llm_max_repair_attempts + 1):
            try:
                identifier = await budget.reserve(
                    settings,
                    inputs,
                    clock=clock,
                    mode=mode,
                    correlation_id=correlation_id,
                    attempt=attempt,
                    task=task,
                    decision_binding=decision_binding,
                )
            except ProviderFailure as error:
                reason = error.code
                await denied(reason, clock=clock, mode=mode, correlation_id=correlation_id)
                break
            reply, review, outcome = None, None, "SUCCESS"
            started = perf_counter()
            try:
                reply = await asyncio.wait_for(
                    provider.review(inputs, repair=attempt > 0, **({"task": task} if task else {})),
                    timeout=min(settings.llm_timeout_seconds, settings.paper_cycle_seconds),
                )
                if reply.outcome != "SUCCESS":
                    raise ProviderFailure(reply.outcome)
                review = _parse_task(reply.raw, inputs, task)
            except (ProviderFailure, asyncio.TimeoutError) as error:
                outcome = error.code if isinstance(error, ProviderFailure) else "TIMEOUT"
            await finish(
                identifier,
                settings,
                clock=clock,
                mode=mode,
                reply=reply,
                review=review,
                outcome=outcome,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
            )
            if review is not None:
                return identifier, _task_result(identifier, review, inputs, task)
            reason = outcome
            if outcome not in {"SCHEMA_ERROR", "TIMEOUT", "RATE_LIMITED", "PROVIDER_ERROR"}:
                break
            if attempt < settings.llm_max_repair_attempts:
                await asyncio.sleep(0.1)
    elif not isinstance(provider, DeterministicFallbackProvider):
        reason = "OPENAI_NOT_IMPLEMENTED"
        await denied(reason, clock=clock, mode=mode, correlation_id=correlation_id)
    return await record_fallback(
        inputs, clock=clock, mode=mode, correlation_id=correlation_id, reason=reason
    )


async def _calendar_context(inputs, mode):
    calendar = None
    if mode == TradingMode.PAPER:
        async with db_session.session_scope() as session:
            resolved = await event_state(
                session,
                inputs.signal.instrument_id,
                inputs.signal.data_origin,
                strategy_id=inputs.signal.strategy_id,
            )
        if resolved.event_id:
            calendar = resolved
    return inputs.model_copy(update={"event_control": calendar})


def _registered_task(task, decision_binding):
    registered = REGISTRY.get(task.name, task.version)
    if (
        registered.digest() != task.digest()
        or not decision_binding
        or registered.digest() != PROPOSAL_AGENT.digest()
    ):
        raise ValueError("unregistered or unbound advisory task")
    return registered


def _parse_task(raw, inputs, task):
    if task is None:
        return parse_review(raw, inputs)
    try:
        return validate_proposal_agent(decode_json(raw), inputs)
    except (ValueError, TypeError, RecursionError) as error:
        if str(error) in {"UNGROUNDED_NUMBER", "UNGROUNDED_PROPOSAL"}:
            raise ProviderFailure(str(error)) from error
        raise LLMSchemaError() from error


def _task_result(identifier, review, inputs, task):
    if review.action != "CONTINUE":
        return None
    if task is not None:
        return ReceiptProposal(call_id=identifier)
    return (
        DeterministicFallbackProvider()
        .propose(inputs)
        .model_copy(update={"thesis": redact_data(review.rationale)})
    )
