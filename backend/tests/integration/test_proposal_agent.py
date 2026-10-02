"""Only the remote HTTP boundary is synthetic; receipts, decisions and storage are real."""

import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.agents.validation import ProposalValidator
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.fundamental_versions import FundamentalVersion
from app.db.models.llm import LLMCall
from app.llm.receipts import ReceiptError
from app.strategies.registry import StrategyRegistry
from tests.integration.test_pipeline import setup_context, time_control
from tests.integration.test_proposal import validator
from tests.llm_fixture import receipt
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload
from tests.unit.test_strategies import FixtureStrategy

__all__ = ["time_control"]


async def test_raw_payload_cannot_assert_model_availability(db_engine):
    context = await setup_context()
    result = await DecisionPipeline(validator()).process(payload(), context)
    assert result.code == "LLM_RECEIPT_REQUIRED" and result.approved_quantity == 0
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_receipt_consumption_survives_restart(db_engine, fake_clock, monkeypatch):
    context = await setup_context()
    reference = await receipt(context, fake_clock, monkeypatch)
    pipeline = DecisionPipeline(validator())
    result = await pipeline.process(reference, context.model_copy(update={"llm_available": False}))
    assert result.code == "RISK_APPROVED" and result.approved_quantity == 250
    await db_session.dispose_engine()
    db_session.init_engine()
    repeated = await pipeline.process(reference, context)
    assert repeated.code == "LLM_RECEIPT_INVALID"
    async with db_session.session_scope() as session:
        proposal = await session.get(Proposal, result.proposal_id)
        assert proposal.origin == "LLM" and proposal.suggested_quantity == 9999
        assert proposal.llm_call_id == reference.call_id
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 1


@pytest.mark.parametrize(
    "change",
    [
        "raw",
        "parsed",
        "request",
        "context",
        "future",
        "stale",
        "audit",
        "source",
        "purpose",
    ],
)
async def test_invalid_receipt_blocks_decision(db_engine, fake_clock, monkeypatch, change):
    context = await setup_context()
    reference = await receipt(context, fake_clock, monkeypatch)
    async with db_session.session_scope() as session:
        call = await session.get(LLMCall, reference.call_id)
        if change == "raw":
            call.raw_response += " "
        elif change == "parsed":
            call.parsed_response = {"action": "ABSTAIN", "proposal": None}
        elif change == "request":
            call.request_context = {**call.request_context, "decision_binding": "tampered"}
        elif change == "purpose":
            call.purpose = "reference_signal_review"
        elif change == "audit":
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.chain_id == call.id)
            )
            event.record_hash = "0" * 64
        elif change == "source":
            source = await session.get(FundamentalVersion, "fund-test")
            source.payload = {**source.payload, "changed_after_request": True}
    if change == "context":
        context = context.model_copy(update={"cycle_id": "different"})
    if change == "future":
        fake_clock.advance(timedelta(seconds=-1))
    if change == "stale":
        fake_clock.advance(timedelta(minutes=5))
    result = await DecisionPipeline(ProposalValidator(validator().policy, fake_clock)).process(
        reference, context
    )
    assert result.code == ("STALE_AFTER_ADVISORY" if change == "stale" else "LLM_RECEIPT_INVALID")
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


async def test_concurrent_receipt_claim_is_atomic(db_engine, fake_clock, monkeypatch):
    context = await setup_context()
    reference = await receipt(context, fake_clock, monkeypatch)

    async def claim(identifier):
        async with db_session.session_scope() as session:
            return await DecisionPipeline(validator())._link_advisory(
                session, reference.call_id, identifier
            )

    outcomes = await asyncio.gather(
        claim("fixture-first"), claim("fixture-second"), return_exceptions=True
    )
    assert sum(isinstance(result, LLMCall) for result in outcomes) == 1
    assert sum(isinstance(result, ReceiptError) for result in outcomes) == 1


async def test_receipt_rechecked_inside_proposal_transaction(db_engine, fake_clock, monkeypatch):
    context = await setup_context()
    reference = await receipt(context, fake_clock, monkeypatch)
    pipeline = DecisionPipeline(validator())
    link = pipeline._link_advisory

    async def altered(session, advisory_id, proposal_id):
        call = await link(session, advisory_id, proposal_id)
        call.parsed_response = {"tampered_during_claim": True}
        await session.flush()
        return call

    monkeypatch.setattr(pipeline, "_link_advisory", altered)
    outcome = await pipeline.process(reference, context)
    assert outcome.code == "LLM_RECEIPT_INVALID"
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0
        assert (await session.get(LLMCall, reference.call_id)).proposal_id is None


async def test_llm_dependency_requires_receipt_not_availability_flag(
    db_engine, fake_clock, monkeypatch
):
    context = await setup_context()
    specification = context.strategy.model_copy(update={"version": "2", "requires_llm": True})
    await StrategyRegistry().register(FixtureStrategy(specification), enabled_paper=True)
    context = context.model_copy(update={"strategy": specification, "llm_available": True})
    pipeline = DecisionPipeline(validator())
    rejected = await quant_decision(pipeline, payload(), context)
    assert rejected.code == "LLM_RECEIPT_REQUIRED" and rejected.approved_quantity == 0
    reference = await receipt(context, fake_clock, monkeypatch)
    approved = await pipeline.process(
        reference, context.model_copy(update={"llm_available": False})
    )
    assert approved.code == "RISK_APPROVED" and approved.approved_quantity == 250


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_version": "unregistered"},
        {"timeframe_seconds": 300},
        {"instrument_key": "NSE:OTHER"},
        {"generated_at": None},
    ],
)
async def test_receipt_signal_must_match_bound_context(db_engine, fake_clock, monkeypatch, changes):
    context = await setup_context()
    if "generated_at" in changes:
        changes = {"generated_at": context.market.as_of - timedelta(minutes=1)}
    reference = await receipt(context, fake_clock, monkeypatch, signal_changes=changes)
    outcome = await DecisionPipeline(validator()).process(reference, context)
    assert outcome.code == "LLM_RECEIPT_INVALID" and outcome.approved_quantity == 0
