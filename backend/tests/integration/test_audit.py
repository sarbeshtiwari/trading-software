"""SQLite integrity tests; PostgreSQL append-only triggers remain separately gated."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.clock import FakeClock
from app.core.logging import register_secret
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal, SizingRecord
from app.db.models.fundamental_versions import FundamentalVersion
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload


@pytest.fixture(autouse=True)
def clock_control(fake_clock):
    fake_clock.set_to(OBSERVED)
    yield


def identity():
    return AuditIdentity(chain_id="chain-test", event_type="TEST", actor="test-only", mode="PAPER")


async def test_audit_hash_chain(db_engine):
    service = AuditService(FakeClock(OBSERVED))
    first = await service.append(
        identity(), {"decision": "REJECTED", "data_used": {"price": Decimal("100.05")}}
    )
    await service.append(identity(), {"decision": "APPROVED"})
    rows = await service.chain("chain-test")
    assert [row.sequence for row in rows] == [1, 2]
    assert rows[1].previous_hash == rows[0].record_hash
    assert await service.verify("chain-test", expected_count=2, expected_head=rows[-1].record_hash)
    assert not await service.verify("chain-test", expected_count=3)
    async with db_session.session_scope() as session:
        row = await session.get(AuditEvent, first)
        row.decision = "TAMPERED"
    assert not await service.verify("chain-test")
    with pytest.raises(ValueError, match="integrity"):
        await service.append(identity(), {})


async def test_audit_snapshot_immutability_and_redaction(db_engine):
    service = AuditService()
    register_secret("test-secret-value-for-redaction")
    values = {
        "price": Decimal("100.05001"),
        "nested": {"password": "test-password"},
        "text": "test-secret-value-for-redaction",
    }
    await service.append(identity(), {"data_used": values})
    values["price"] = Decimal(1)
    row = (await service.chain("chain-test"))[0]
    assert row.data_used["price"] == "100.05001"
    assert "test-password" not in str(row.data_used)
    assert "test-secret-value-for-redaction" not in str(row.data_used)
    assert await service.verify("chain-test")
    with pytest.raises(ValueError):
        freeze_snapshot({"price": Decimal("NaN")})
    with pytest.raises(ValueError, match="reserved"):
        await service.append(identity(), {"record_hash": "forged"})


async def test_audit_negative_decisions(db_engine):
    context = await setup_context()
    outcome = await DecisionPipeline(validator()).process(payload(confidence="0.5"), context)
    service = AuditService()
    rows = await service.chain(outcome.candidate_id)
    assert len(rows) == 1 and rows[0].decision == "REJECTED"
    assert rows[0].risk_verdict == "REJECTED"
    assert rows[0].result["reason_code"] == "CONFIDENCE_BELOW_FLOOR"
    assert rows[0].signal["confidence"] == "0.5"
    assert rows[0].data_used["validation_policy"]["confidence_floor"] == "0.6"
    assert "thesis" not in rows[0].signal
    assert await service.verify(outcome.candidate_id)


async def test_pipeline_positive_audit_and_failure_atomicity(db_engine, monkeypatch):
    context = await setup_context()
    pipeline = DecisionPipeline(validator())
    outcome = await quant_decision(pipeline, payload(), context)
    row = (await AuditService().chain(outcome.proposal_id))[0]
    assert row.position_size["quantity"] == 250 and row.risk_calculation["approved"] is True

    async def broken(*args, **kwargs):
        raise RuntimeError("injected audit storage failure")

    monkeypatch.setattr(AuditService, "append_in_session", broken)
    with pytest.raises(RuntimeError, match="audit storage"):
        await quant_decision(pipeline, payload(), context)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SizingRecord)) == 1


async def test_corrupt_head_and_missing_tail_detected(db_engine):
    service = AuditService()
    await service.append(identity(), {"decision": "REJECTED"})
    rows = await service.chain("chain-test")
    head = rows[0].record_hash
    async with db_session.session_scope() as session:
        row = await session.get(AuditEvent, rows[0].id)
        row.record_hash = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        await service.append(identity(), {})
    assert not await service.verify("chain-test", expected_head=head)
    assert not await service.verify("missing", expected_count=1)
    assert not await service.verify("missing")


async def test_audit_column_limits_match_postgres(db_engine):
    with pytest.raises(ValueError, match="text field"):
        await AuditService().append(identity(), {"risk_verdict": "x" * 33})


async def test_source_change_does_not_change_audit_snapshot(db_engine):
    context = await setup_context()
    outcome = await quant_decision(DecisionPipeline(validator()), payload(), context)
    async with db_session.session_scope() as session:
        row = await session.get(FundamentalVersion, "fund-test")
        row.payload = {"test_only": "changed after decision"}
    service = AuditService()
    audit = (await service.chain(outcome.proposal_id))[0]
    assert audit.data_used["validated_evidence"]["FUNDAMENTAL:fund-test"]["payload"] == {
        "test_only": True
    }
    assert "inputs" in audit.data_used["regime_snapshot"]
    assert await service.verify(outcome.proposal_id)
