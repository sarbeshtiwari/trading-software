"""Full-precision persisted sizing replay and corruption detection."""

import pytest

from app.db import session as db_session
from app.db.models.decision import SizingRecord
from app.sizing.audit import SizingAudit
from tests.unit.test_sizing import evidence, policy


async def test_sizing_audit_replay(db_engine):
    audit = SizingAudit()
    identifier, result = await audit.calculate_and_record(
        evidence(risk_cost_per_unit="0.333333"), policy()
    )
    assert result.quantity == 214
    assert await audit.replay(identifier) == result
    async with db_session.session_scope() as session:
        row = await session.get(SizingRecord, identifier)
        assert row.inputs["request"]["risk_cost_per_unit"] == "0.333333"
        assert row.inputs["request"]["data_origin"] == "SYNTHETIC"
        row.final_quantity += 1
    with pytest.raises(ValueError, match="MISMATCH"):
        await audit.replay(identifier)


async def test_zero_sizing_recorded(db_engine):
    audit = SizingAudit()
    identifier, result = await audit.calculate_and_record(evidence(available_margin=0), policy())
    assert result.quantity == 0
    assert (await audit.replay(identifier)).zero_reason == "BUDGET_BELOW_MIN_LOT"
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        await audit.replay("missing")


async def test_defined_loss_replay_survives_restart_and_cannot_claim_legacy_formula(db_engine):
    audit = SizingAudit()
    identifier, result = await audit.calculate_and_record(
        evidence(defined_max_loss_per_unit=100, lot_size=5), policy()
    )
    assert result.quantity == 5 and result.formula_version == "1.1.0"
    await db_session.dispose_engine()
    db_session.init_engine()
    assert await audit.replay(identifier) == result
    async with db_session.session_scope() as session:
        row = await session.get(SizingRecord, identifier)
        assert row.inputs["request"]["defined_max_loss_per_unit"] == "100"
        row.formula_version = "1.0.0"
    with pytest.raises(ValueError, match="MISMATCH"):
        await audit.replay(identifier)
