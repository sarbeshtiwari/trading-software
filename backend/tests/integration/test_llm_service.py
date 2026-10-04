"""Durable reservations and the actual shared decision path; HTTP alone is a fixture."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.agents.pipeline import DecisionPipeline
from app.agents.proposal import EvidenceReference
from app.agents.validation import ProposalValidator
from app.audit.service import AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.journal import JournalEntry
from app.db.models.llm import LLMBudgetDay, LLMCall
from app.db.models.trading import Order, Position
from app.llm import budget
from app.llm.base import ProviderFailure, ProviderReply
from app.llm.claude import ClaudeProvider
from app.llm.fallback import DeterministicFallbackProvider, ProposalInputs
from app.llm.schema import parse_review
from app.llm.service import finish, review_signal
from app.modes import TradingMode
from app.strategies.signal import Signal
from tests.conftest import BACKEND_ROOT
from tests.integration.test_event_controls import PATH, publication
from tests.integration.test_pipeline import setup_context, signal_data, time_control
from tests.integration.test_proposal import validator
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.unit.test_llm import inputs, response, settings

__all__ = ["credentials", "time_control"]


def install_http(monkeypatch, handler):
    monkeypatch.setattr(
        "app.llm.factory.ClaudeProvider",
        lambda config: ClaudeProvider(config, transport=httpx.MockTransport(handler)),
    )


async def review(config, fake_clock, data=None):
    return await review_signal(
        data or inputs(),
        config,
        clock=fake_clock,
        mode=TradingMode.PAPER,
        correlation_id="fixture-cycle",
    )


async def test_usage_accounting_and_idempotent_settlement(db_engine, fake_clock, monkeypatch):
    install_http(monkeypatch, lambda request: httpx.Response(200, json=response()))
    identifier, proposal = await review(settings(), fake_clock)
    assert proposal.entry == 100 and proposal.quantity == 1
    async with db_session.session_scope() as session:
        call = await session.get(LLMCall, identifier)
        quota = await session.get(LLMBudgetDay, fake_clock.utcnow().date())
        assert call.cost_usd == Decimal("0.000300")
        assert parse_review(call.raw_response, inputs()).action == "CONTINUE"
        assert call.request_context["request"] == ClaudeProvider(settings()).request(
            inputs(), False
        )
        assert quota.spent_microusd == 300 and quota.reserved_microusd == 0
        assert quota.spent_tokens == 110 and quota.reserved_tokens == 0
    with pytest.raises(RuntimeError, match="already settled"):
        await finish(
            identifier,
            settings(),
            clock=fake_clock,
            mode=TradingMode.PAPER,
            reply=ProviderReply(response()["content"][0]["text"], 100, 10),
            review=None,
            outcome="SUCCESS",
            latency_ms=1,
        )
    async with db_session.session_scope() as session:
        assert (await session.get(LLMBudgetDay, fake_clock.utcnow().date())).spent_microusd == 300
        (await session.get(LLMBudgetDay, fake_clock.utcnow().date())).spent_microusd = -1

    def forbidden(request):
        pytest.fail("negative counters must not authorize a paid call")

    install_http(monkeypatch, forbidden)
    fallback_id, proposal = await review(settings(), fake_clock)
    assert proposal.quantity == 1
    async with db_session.session_scope() as session:
        assert (await session.get(LLMCall, fallback_id)).request_context[
            "reason"
        ] == "BUDGET_EXCEEDED"


async def test_reservation_concurrency_restart_and_clock_rollback(db_engine, fake_clock):
    config = settings(llm_daily_token_cap=1002048)

    async def reserve():
        return await budget.reserve(
            config,
            inputs(),
            clock=fake_clock,
            mode=TradingMode.PAPER,
            correlation_id="fixture-cycle",
            attempt=0,
        )

    results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, ProviderFailure) for result in results) == 1
    await db_session.dispose_engine()
    db_session.init_engine()
    fake_clock.advance(timedelta(seconds=40))
    with pytest.raises(ProviderFailure, match="BUDGET_EXCEEDED"):
        await reserve()
    async with db_session.session_scope() as session:
        call = await session.scalar(sa.select(LLMCall))
        assert call.outcome == "PENDING" and call.cost_usd is None
        quota = await session.get(LLMBudgetDay, fake_clock.utcnow().date())
        assert quota.reserved_microusd == 2020480 and quota.reserved_tokens == 1002048
    fake_clock.advance(timedelta(seconds=-50))
    with pytest.raises(ProviderFailure, match="CIRCUIT_OPEN_OR_BUSY"):
        await reserve()


async def test_rate_limit_storm_opens_durable_circuit_without_blocking_fallback(
    db_engine, fake_clock, monkeypatch
):
    attempts = []

    def limited(request):
        attempts.append(request)
        return httpx.Response(429, json={"error": "isolated"})

    install_http(monkeypatch, limited)
    config = settings(
        llm_circuit_failure_threshold=2, llm_daily_token_cap=10000000, llm_daily_cost_cap_usd=20
    )
    assert (await review(config, fake_clock))[1].entry == 100
    assert len(attempts) == 2
    await db_session.dispose_engine()
    db_session.init_engine()
    assert (await review(config, fake_clock))[1].entry == 100
    assert len(attempts) == 2
    async with db_session.session_scope() as session:
        remote = list(
            (await session.scalars(sa.select(LLMCall).where(LLMCall.provider == "claude"))).all()
        )
        assert len(remote) == 2 and all(call.cost_usd is None for call in remote)
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "NOTIFICATION_REQUESTED")
            )
            >= 2
        )
    fake_clock.advance(timedelta(seconds=61))
    install_http(monkeypatch, lambda request: httpx.Response(200, json=response()))
    identifier, proposal = await review(config, fake_clock)
    assert proposal is not None
    async with db_session.session_scope() as session:
        assert (await session.get(LLMCall, identifier)).outcome == "SUCCESS"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"llm_input_usd_per_million": None}, "TARIFF_OR_MODEL_UNAVAILABLE"),
        ({"llm_daily_cost_cap_usd": "1"}, "BUDGET_EXCEEDED"),
        ({"llm_daily_token_cap": 1}, "BUDGET_EXCEEDED"),
    ],
)
async def test_denied_budget_never_calls_http(db_engine, fake_clock, monkeypatch, changes, code):
    def forbidden(request):
        raise AssertionError("HTTP must not run before reservation")

    install_http(monkeypatch, forbidden)
    identifier, proposal = await review(settings(**changes), fake_clock)
    assert proposal.quantity == 1
    async with db_session.session_scope() as session:
        call = await session.get(LLMCall, identifier)
        assert call.provider == "fallback" and call.request_context["reason"] == code
        notices = list(await session.scalars(sa.select(AuditEvent).where(
            AuditEvent.event_type == "NOTIFICATION_REQUESTED"
        )))
        assert len(notices) == 1
        notice = notices[0].result["notification"]
        assert notice["event_type"] == (
            "LLM_BUDGET_EXHAUSTED" if code == "BUDGET_EXCEEDED" else "LLM_DEGRADED"
        )
        assert notice["severity"] == "WARNING"
        source = await session.get(AuditEvent, notices[0].result["source_audit_id"])
        assert source.result["reason"] == code
        assert source.correlation_id == call.correlation_id
        assert await session.scalar(sa.select(sa.func.count()).select_from(LLMCall).where(
            LLMCall.provider == "claude"
        )) == 0


async def test_grounding_failure_bounded_repair_and_safe_quant_fallback(
    db_engine, fake_clock, monkeypatch
):
    requests = []

    def malformed(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(raw='{"action":"CONTINUE","risk_override":true}'))

    install_http(monkeypatch, malformed)
    identifier, proposal = await review(settings(), fake_clock)
    assert proposal.quantity == 1 and len(requests) == 2
    assert "Previous response was invalid" in requests[1]["system"]
    async with db_session.session_scope() as session:
        assert (await session.get(LLMCall, identifier)).provider == "fallback"
        remote = list(
            (await session.scalars(sa.select(LLMCall).where(LLMCall.provider == "claude"))).all()
        )
        assert all(call.outcome == "SCHEMA_ERROR" and call.schema_valid is False for call in remote)


@pytest.mark.parametrize(
    "action,advance,expected",
    [
        ("CONTINUE", 0, "RISK_APPROVED"),
        ("ABSTAIN", 0, "ADVISORY_ABSTAIN"),
        ("CONTINUE", 90, "STALE_AFTER_ADVISORY"),
    ],
)
async def test_actual_pipeline_review_does_not_own_sizing_or_freshness(
    db_engine, fake_clock, monkeypatch, action, advance, expected
):
    context = (await setup_context()).model_copy(update={"llm_available": False})
    config = settings()
    monkeypatch.setattr("app.agents.pipeline.get_settings", lambda: config)

    def reply(request):
        fake_clock.advance(timedelta(seconds=advance))
        payload = response(action=action)
        review_data = json.loads(payload["content"][0]["text"])
        review_data["evidence_ids"] = ["fund-test"]
        payload["content"][0]["text"] = json.dumps(review_data)
        return httpx.Response(200, json=payload)

    install_http(monkeypatch, reply)
    result = await DecisionPipeline(ProposalValidator(validator().policy, fake_clock)).process(
        Signal(**signal_data()),
        context,
        evidence=(EvidenceReference(kind="FUNDAMENTAL", source_id="fund-test"),),
        archive_fallback=True,
    )
    assert result.code == expected
    assert result.approved_quantity == (250 if expected == "RISK_APPROVED" else 0)


async def test_historical_review_cannot_use_future_model_knowledge(
    db_engine, fake_clock, monkeypatch
):
    original = inputs()
    historic = original.model_copy(
        update={"signal": original.signal.model_copy(update={"data_origin": DataOrigin.HISTORICAL})}
    )
    monkeypatch.setattr(
        "app.llm.service.provider_for", lambda config: pytest.fail("no model in historical run")
    )
    identifier, proposal = await review(settings(), fake_clock, historic)
    assert proposal.entry == 100
    async with db_session.session_scope() as session:
        assert "NO_LOOKAHEAD" in (await session.get(LLMCall, identifier)).request_context["reason"]


@pytest.mark.parametrize("action", ["CONTINUE", "ABSTAIN", "TIMEOUT"])
@pytest.mark.parametrize("task", ["review", "proposal"])
async def test_real_worker_with_claude_boundary_and_fallback(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, action, task
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    calendar = await client.put(PATH, json=publication(fake_clock, delay=30))
    assert calendar.status_code == 200, calendar.text
    for name, value in {
        "LLM_PROVIDER": "claude",
        "LLM_REFERENCE_REVIEW_ENABLED": "true",
        "LLM_REFERENCE_TASK": task,
        "ANTHROPIC_API_KEY": "isolated-fixture-key",
        "LLM_TARIFF_MODEL": "claude-sonnet-5",
        "LLM_INPUT_USD_PER_MILLION": "2",
        "LLM_OUTPUT_USD_PER_MILLION": "10",
        "LLM_TARIFF_VALID_UNTIL": "2027-01-01T00:00:00Z",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    attempts = []

    def http(request):
        attempts.append(request)
        if action == "TIMEOUT":
            raise httpx.ReadTimeout("isolated timeout", request=request)
        payload = json.loads(request.content)
        data = json.loads(
            payload["messages"][0]["content"]
            .removeprefix("<UNTRUSTED_DATA>")
            .removesuffix("</UNTRUSTED_DATA>")
        )
        assert data["grounded_evidence"]
        assert data["event_control"]["event_id"] == calendar.json()["event_id"]
        assert data["event_control"]["status"] == "CLEAR"
        assert data["event_control"]["calendars"][0]["events"][0]["id"] == "fixture-policy"
        assert data["signal"]["entry"] == "100"
        assert len(data["evidence"]) == 1 and data["evidence"][0]["kind"] == "MARKET"
        body = response(action=action)
        body["content"][0]["text"] = json.dumps(
            {
                "action": action,
                "rationale": "Isolated recorded-market review fixture",
                "evidence_ids": [data["evidence"][0]["source_id"]],
            }
        )
        if task == "proposal":
            proposal = DeterministicFallbackProvider().propose(ProposalInputs.model_validate(data))
            body["content"][0]["text"] = json.dumps(
                {
                    "action": action,
                    "proposal": proposal.model_copy(
                        update={
                            "quantity": 9999,
                            "thesis": "Recorded market evidence supports the hypothesis",
                        }
                    ).model_dump(mode="json")
                    if action == "CONTINUE"
                    else None,
                }
            )
        return httpx.Response(200, json=body)

    install_http(monkeypatch, http)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            order_count = await session.scalar(sa.select(sa.func.count()).select_from(Order))
            assert order_count == (0 if action == "ABSTAIN" else 1)
            if action != "ABSTAIN":
                assert (await session.scalar(sa.select(Position))).net_quantity == 111
                order = await session.scalar(sa.select(Order))
                proposal = await session.get(Proposal, order.proposal_id)
                is_model_proposal = task == "proposal" and action == "CONTINUE"
                assert proposal.origin == ("LLM" if is_model_proposal else "QUANT")
                assert proposal.suggested_quantity == (9999 if is_model_proposal else None)
        assert len(attempts) == 1
        if action != "ABSTAIN":
            quote = provider.get_quote.return_value
            fake_clock.advance(timedelta(seconds=1))
            provider.get_quote.return_value = replace(
                quote,
                observed_at=fake_clock.now(),
                ltp=Decimal(108),
                bids=(replace(quote.bids[0], price=Decimal(108)),),
                asks=(replace(quote.asks[0], price=Decimal("108.05")),),
            )
            await worker.cycle()
            assert not worker.failed, worker.detail
            async with db_session.session_scope() as session:
                assert (await session.scalar(sa.select(JournalEntry))).net_pnl == Decimal("856.56")
        view = (await client.get("/api/v1/workspace")).json()
        assert view["advisory_budget"]["review_enabled"] is True
        remote = [row for row in view["advisory_calls"] if row["provider"] == "claude"]
        assert len(remote) == 1
        assert (
            all(row["cost_usd"] is None for row in remote)
            if action == "TIMEOUT"
            else all(Decimal(row["cost_usd"]) == Decimal("0.000300") for row in remote)
        )
        assert Decimal(view["advisory_budget"]["reserved_usd"]) == (
            Decimal("2.020480") if action == "TIMEOUT" else 0
        )
    finally:
        await worker.stop()
        await client.aclose()


def test_budget_migration_preserves_unknown_costs(settings_env, tmp_path):
    path = tmp_path / "llm-migration.db"
    settings_env(DATABASE_URL=f"sqlite+aiosqlite:///{path}")
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(LLMCall),
                {
                    "id": "isolated-migration-call",
                    "provider": "claude",
                    "model_id": "fixture",
                    "purpose": "fixture",
                    "prompt_version": "1",
                    "prompt_hash": "0" * 64,
                    "outcome": "PENDING",
                    "cost_usd": None,
                    "called_at": get_clock().utcnow(),
                },
            )
        with pytest.raises(RuntimeError, match="unavailable LLM costs"):
            command.downgrade(config, "0010_journal_revisions")
        with engine.begin() as connection:
            assert connection.scalar(sa.select(LLMCall.cost_usd)) is None
            connection.execute(sa.delete(LLMCall).where(LLMCall.id == "isolated-migration-call"))
        command.downgrade(config, "0010_journal_revisions")
        with engine.begin() as connection:
            assert "llm_budget_days" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()


async def test_response_audit_failure_retains_unsettled_reservation(
    db_engine, fake_clock, monkeypatch
):
    install_http(monkeypatch, lambda request: httpx.Response(200, json=response()))
    original = AuditService.append_in_session

    async def fail_result(self, session, identity, details, **kwargs):
        if identity.event_type == "ADVISORY_PROPOSAL":
            raise RuntimeError("isolated response audit storage failure")
        return await original(self, session, identity, details, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail_result)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        await review(settings(), fake_clock)
    async with db_session.session_scope() as session:
        call = await session.scalar(sa.select(LLMCall))
        quota = await session.get(LLMBudgetDay, fake_clock.utcnow().date())
        assert call.outcome == "PENDING" and call.cost_usd is None
        assert quota.reserved_microusd == 2020480 and quota.spent_microusd == 0


async def test_reference_review_wait_is_bounded_by_worker_cycle(db_engine, fake_clock, monkeypatch):
    cancelled = asyncio.Event()

    async def slow(request):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return httpx.Response(200, json=response())

    install_http(monkeypatch, slow)
    identifier, proposal = await review(
        settings(
            paper_cycle_seconds=1,
            llm_timeout_seconds=30,
            llm_max_repair_attempts=0,
        ),
        fake_clock,
    )
    assert cancelled.is_set() and proposal.quantity == 1
    async with db_session.session_scope() as session:
        assert (await session.get(LLMCall, identifier)).provider == "fallback"
        remote = await session.scalar(sa.select(LLMCall).where(LLMCall.provider == "claude"))
        assert remote.outcome == "TIMEOUT" and remote.cost_usd is None
        assert remote.request_context["settings"]["effective_timeout_seconds"] == 1
