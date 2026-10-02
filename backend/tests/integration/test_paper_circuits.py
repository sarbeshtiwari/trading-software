"""Supplied fixture circuit bands reach real OMS preflight and PAPER fills."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


@pytest.mark.parametrize(
    "lower,upper,code",
    [
        ("90", "99.95", "PRICE_OUTSIDE_CIRCUIT_BAND"),
        ("101", "110", "PRICE_OUTSIDE_CIRCUIT_BAND"),
        ("110", "90", "INVALID_CIRCUIT_BAND"),
        (None, "110", "INVALID_CIRCUIT_BAND"),
    ],
)
async def test_preflight_circuit_rejection_never_calls_broker(
    db_engine, credentials, fake_clock, monkeypatch, lower, upper, code
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    submit = AsyncMock(wraps=engine.broker.place_order)
    monkeypatch.setattr(engine.broker, "place_order", submit)
    market.update(lower_circuit=Decimal(lower) if lower else None, upper_circuit=Decimal(upper))
    try:
        with pytest.raises(SafetyError, match=code):
            await engine.submit(proposal)
        submit.assert_not_awaited()
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.event_type == "ENTRY_CIRCUIT_PREFLIGHT",
                    AuditEvent.proposal_id == proposal,
                )
            )
            assert event.result["status"] == code
            assert event.result["upper"] == upper
    finally:
        await client.aclose()


async def test_exact_circuit_boundaries_allow_real_paper_fill(db_engine, credentials, fake_clock):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    market.update(lower_circuit=Decimal("99.95"), upper_circuit=Decimal("100"))
    try:
        await engine.submit(proposal)
        orders = await engine.broker.list_orders()
        assert len(orders) == 1
        assert orders[0].filled_quantity == 250
    finally:
        await client.aclose()
