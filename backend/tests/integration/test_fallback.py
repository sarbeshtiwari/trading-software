"""Rule-derived advisory output still faces the actual shared safety gates."""

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.agents.proposal import EvidenceReference, TradeProposal
from app.core.logging import register_secret
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.llm import LLMCall
from app.strategies.signal import Signal
from tests.integration.test_pipeline import setup_context, signal_data, time_control
from tests.integration.test_proposal import validator

__all__ = ["time_control"]


@pytest.mark.parametrize("confidence", ["0.9", "0.1"])
async def test_fallback_archival_shared_validation_and_redaction(db_engine, confidence):
    context = (await setup_context()).model_copy(update={"llm_available": False})
    register_secret("private-fixture-secret")
    result = await DecisionPipeline(validator()).process(
        Signal(**(signal_data() | {
            "confidence": confidence,
            "conditions_fired": ("closed breakout private-fixture-secret",),
        })),
        context,
        evidence=(EvidenceReference(kind="FUNDAMENTAL", source_id="fund-test"),),
        archive_fallback=True,
    )
    assert result.code == ("RISK_APPROVED" if confidence == "0.9" else "CONFIDENCE_BELOW_FLOOR")
    assert result.approved_quantity == (250 if confidence == "0.9" else 0)
    async with db_session.session_scope() as session:
        calls = list((await session.scalars(sa.select(LLMCall))).all())
        assert len(calls) == 1
        call = calls[0]
        assert "private-fixture-secret" not in str(call.request_context)
        assert "private-fixture-secret" not in call.raw_response
        assert "private-fixture-secret" not in str(call.parsed_response)
        assert TradeProposal.model_validate_json(call.raw_response).quantity == 1
        assert call.proposal_id == result.proposal_id
        if result.proposal_id:
            proposal = await session.get(Proposal, result.proposal_id)
            assert proposal.llm_call_id == call.id
            assert proposal.origin == "QUANT" and proposal.suggested_quantity is None


async def test_fallback_receipt_failure_prevents_proposal(db_engine, monkeypatch):
    context = await setup_context()

    async def unavailable(*args, **kwargs):
        raise RuntimeError("isolated receipt storage failure")

    monkeypatch.setattr("app.agents.pipeline.record_fallback", unavailable)
    with pytest.raises(RuntimeError, match="receipt storage failure"):
        await DecisionPipeline(validator()).process(
            Signal(**signal_data()), context, archive_fallback=True
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0
