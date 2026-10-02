"""Atomic conservative reservations survive timeout, concurrency and process death."""

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical, freeze_snapshot
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.llm import LLMBudgetDay, LLMCall, LLMProviderState
from app.llm import prompts
from app.llm.base import ProviderFailure
from app.llm.claude import ClaudeProvider

INPUT_CEILING = 1000000
SUPPORTED_MODEL = "claude-sonnet-5"


def tariff(settings, now):
    try:
        deadline = datetime.fromisoformat(
            (settings.llm_tariff_valid_until or "").replace("Z", "+00:00")
        )
        if (
            settings.llm_model != SUPPORTED_MODEL
            or settings.llm_tariff_model != settings.llm_model
            or deadline.utcoffset() is None
            or deadline <= now
            or settings.llm_input_usd_per_million is None
            or settings.llm_output_usd_per_million is None
        ):
            raise ValueError("tariff unavailable")
        return {
            "model": settings.llm_model,
            "input_usd_per_million": str(settings.llm_input_usd_per_million),
            "output_usd_per_million": str(settings.llm_output_usd_per_million),
            "valid_until": deadline.isoformat(),
            "input_ceiling": INPUT_CEILING,
            "output_ceiling": settings.llm_max_output_tokens,
            "basis": "OWNER_TARIFF_ESTIMATE_NOT_PROVIDER_INVOICE",
        }
    except (ValueError, TypeError) as error:
        raise ProviderFailure("TARIFF_OR_MODEL_UNAVAILABLE") from error


def cost(schedule, input_tokens, output_tokens):
    return (
        (
            input_tokens * Decimal(schedule["input_usd_per_million"])
            + output_tokens * Decimal(schedule["output_usd_per_million"])
        )
        / Decimal(1000000)
    ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


async def _insert_absent(session, model, values):
    dialect = session.bind.dialect.name
    insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}.get(dialect)
    if insert is None:
        raise ProviderFailure("BUDGET_STORAGE_UNSUPPORTED")
    await session.execute(insert(model).values(**values).on_conflict_do_nothing())


async def reserve(
    settings, inputs, *, clock, mode, correlation_id, attempt, task=None, decision_binding=None
):
    now = clock.utcnow()
    schedule = tariff(settings, now)
    identifier = new_id("llm")
    reserve_cost = cost(schedule, INPUT_CEILING, settings.llm_max_output_tokens)
    reserve_tokens = INPUT_CEILING + settings.llm_max_output_tokens
    request = ClaudeProvider(settings).request(inputs, repair=attempt > 0, task=task)
    state_id = "claude:" + settings.llm_model
    async with db_session.session_scope() as session:
        await _insert_absent(
            session,
            LLMProviderState,
            {
                "id": state_id,
                "failures": 0,
                "last_seen_at": now,
            },
        )
        control = await session.execute(
            sa.update(LLMProviderState)
            .where(
                LLMProviderState.id == state_id,
                LLMProviderState.last_seen_at <= now,
                sa.or_(LLMProviderState.open_until.is_(None), LLMProviderState.open_until <= now),
                sa.or_(LLMProviderState.lease_until.is_(None), LLMProviderState.lease_until <= now),
            )
            .values(
                lease_call_id=identifier,
                lease_until=now + timedelta(seconds=settings.llm_timeout_seconds + 5),
                last_seen_at=now,
            )
        )
        if control.rowcount != 1:
            raise ProviderFailure("CIRCUIT_OPEN_OR_BUSY")
        await _insert_absent(
            session,
            LLMBudgetDay,
            {
                "day": now.date(),
                "reserved_microusd": 0,
                "spent_microusd": 0,
                "reserved_tokens": 0,
                "spent_tokens": 0,
            },
        )
        reserved = await session.execute(
            sa.update(LLMBudgetDay)
            .where(
                LLMBudgetDay.day == now.date(),
                LLMBudgetDay.spent_microusd >= 0,
                LLMBudgetDay.reserved_microusd >= 0,
                LLMBudgetDay.spent_tokens >= 0,
                LLMBudgetDay.reserved_tokens >= 0,
                LLMBudgetDay.spent_microusd
                + LLMBudgetDay.reserved_microusd
                + int(reserve_cost * 1000000)
                <= int(settings.llm_daily_cost_cap_usd * 1000000),
                LLMBudgetDay.spent_tokens + LLMBudgetDay.reserved_tokens + reserve_tokens
                <= settings.llm_daily_token_cap,
            )
            .values(
                reserved_microusd=LLMBudgetDay.reserved_microusd + int(reserve_cost * 1000000),
                reserved_tokens=LLMBudgetDay.reserved_tokens + reserve_tokens,
            )
        )
        if reserved.rowcount != 1:
            raise ProviderFailure("BUDGET_EXCEEDED")
        context = freeze_snapshot(
            {
                "request": request,
                "task": task.model_dump(mode="json") if task else None,
                "inputs": inputs.model_dump(mode="json"),
                "decision_binding": decision_binding,
                "policy_hash": prompts.policy_hash(),
                "tariff": schedule,
                "budget_day": now.date(),
                "reserved_usd": reserve_cost,
                "reserved_tokens": reserve_tokens,
                "provider_state_id": state_id,
                "settings": {
                    "thinking": "disabled",
                    "sampling": "MODEL_FIXED_DEFAULT_ONLY",
                    "effective_timeout_seconds": min(
                        settings.llm_timeout_seconds, settings.paper_cycle_seconds
                    ),
                    "api_version": "2023-06-01",
                },
            }
        )
        session.add(
            LLMCall(
                id=identifier,
                provider="claude",
                model_id=settings.llm_model,
                purpose=task.name if task else "reference_signal_review",
                prompt_version=task.version if task else prompts.VERSION,
                prompt_hash=sha256(canonical(request).encode()).hexdigest(),
                cost_usd=None,
                outcome="PENDING",
                repair_attempts=attempt,
                request_context=context,
                max_output_tokens=settings.llm_max_output_tokens,
                schema_name=task.schema_name if task else "AdvisoryReview",
                schema_valid=False,
                correlation_id=correlation_id,
                called_at=now,
            )
        )
        await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier, event_type="ADVISORY_REQUEST", actor="llm_budget", mode=mode
            ),
            {
                "correlation_id": correlation_id,
                "data_used": context,
                "result": {"outcome": "PENDING"},
            },
        )
    return identifier


async def settle(session, call, settings, *, clock, reply, outcome):
    context = call.request_context
    schedule = context["tariff"]
    accounted = None
    tokens = None
    if reply is not None:
        if (
            type(reply.input_tokens) is int
            and type(reply.output_tokens) is int
            and 0 <= reply.input_tokens <= schedule["input_ceiling"]
            and 0 <= reply.output_tokens <= schedule["output_ceiling"]
        ):
            accounted = cost(schedule, reply.input_tokens, reply.output_tokens)
            tokens = reply.input_tokens + reply.output_tokens
            call.input_tokens, call.output_tokens = reply.input_tokens, reply.output_tokens
    if accounted is not None:
        result = await session.execute(
            sa.update(LLMBudgetDay)
            .where(
                LLMBudgetDay.day == call.called_at.date(),
                LLMBudgetDay.reserved_microusd >= int(Decimal(context["reserved_usd"]) * 1000000),
                LLMBudgetDay.reserved_tokens >= context["reserved_tokens"],
            )
            .values(
                reserved_microusd=LLMBudgetDay.reserved_microusd
                - int(Decimal(context["reserved_usd"]) * 1000000),
                reserved_tokens=LLMBudgetDay.reserved_tokens - context["reserved_tokens"],
                spent_microusd=LLMBudgetDay.spent_microusd + int(accounted * 1000000),
                spent_tokens=LLMBudgetDay.spent_tokens + tokens,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("LLM budget reservation integrity failure")
    call.cost_usd = accounted
    failures = 0 if outcome == "SUCCESS" else LLMProviderState.failures + 1
    await session.execute(
        sa.update(LLMProviderState)
        .where(
            LLMProviderState.id == context["provider_state_id"],
            LLMProviderState.lease_call_id == call.id,
        )
        .values(
            failures=failures,
            lease_call_id=None,
            lease_until=None,
            open_until=sa.case(
                (
                    failures >= settings.llm_circuit_failure_threshold,
                    clock.utcnow() + timedelta(seconds=settings.llm_circuit_cooldown_seconds),
                ),
                else_=None,
            ),
        )
    )
