"""RISK-018: risk is re-evaluated against the latest state before submission.

Existing preflight coverage exercises *contract* changes between proposal and
dispatch — a changed product, a calendar publication, an entry window. What had
no test was the requirement's own acceptance case: the account itself moving
such that a risk limit breaches, while the proposal stays exactly as approved.
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.db.models.trading import Order
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]

MARGIN_CODES = {"INSUFFICIENT_MARGIN", "MARGIN_UTILISATION_EXCEEDED"}


async def preflights():
    async with db_session.session_scope() as session:
        return list(
            (
                await session.scalars(
                    sa.select(RiskDecision).where(RiskDecision.is_preflight.is_(True))
                )
            ).all()
        )


async def test_preexecution_recheck_aborts_when_the_account_moves(
    db_engine, credentials, fake_clock
):
    """Committing the account's margin after approval must abort submission."""
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        # The proposal was approved against an uncommitted account. Commit almost
        # all of the margin now; the proposal itself is untouched.
        engine.broker.account.used_margin = Decimal(99000)
        await engine.broker.persist()

        with pytest.raises(SafetyError) as raised:
            await engine.submit(proposal)
        assert str(raised.value) in MARGIN_CODES, str(raised.value)

        # Nothing reached the broker and no order was created locally.
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0

        # The refusal is sealed as a preflight decision, not merely raised.
        recorded = await preflights()
        assert len(recorded) == 1
        assert not recorded[0].approved
        assert recorded[0].rejection_code in MARGIN_CODES
        assert recorded[0].binding_rule.startswith("margin")
    finally:
        await client.aclose()


async def test_preexecution_recheck_admits_an_unchanged_account(
    db_engine, credentials, fake_clock
):
    """The re-check must not refuse the ordinary path it is protecting."""
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        assert identifier
        assert len(await engine.broker.list_orders()) == 1
        recorded = await preflights()
        assert len(recorded) == 1 and recorded[0].approved
        # The sealed snapshot is what the decision was actually made on.
        assert set(recorded[0].state_snapshot) >= {
            "proposal",
            "portfolio",
            "market",
            "config",
            "decision",
        }
    finally:
        await client.aclose()
