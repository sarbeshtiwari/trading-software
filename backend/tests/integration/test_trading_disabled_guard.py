"""BE-011: entry-creating mutations are locked while trading is disabled.

The guard is deliberately asymmetric. A replacement creates a new entry, so it
is refused outright with 423 and the blocking component named. A cancellation
reduces exposure and must stay usable precisely when trading has been halted —
locking it would trap the owner in a position they are trying to close.
"""

from uuid import uuid4

import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.monitoring.gate import get_trading_gate
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]

BLOCKER = ("health_watchdog", "database check is failing")


def replacement_body():
    return {
        "request_id": str(uuid4()),
        "reason": "Owner attempts a replacement while entries are blocked",
        "confirmation": "REPLACE PAPER ENTRY",
        "replacement_proposal_id": "prp-does-not-matter",
    }


async def test_trading_disabled_guard(db_engine, credentials, fake_clock):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        get_trading_gate().block(*BLOCKER)

        response = await client.post(
            f"/api/v1/orders/{identifier}/replace", json=replacement_body()
        )
        assert response.status_code == 423, response.text
        detail = response.json()["detail"]
        assert BLOCKER[0] in detail and BLOCKER[1] in detail

        # The refusal is audited, not silently dropped.
        async with db_session.session_scope() as session:
            refusals = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "OWNER_REPLACE_REFUSED"
                        )
                    )
                ).all()
            )
        assert len(refusals) == 1
        assert BLOCKER[0] in refusals[0].result["code"]

        # No second order was created by the refused replacement.
        assert len(await engine.broker.list_orders()) == 1
    finally:
        get_trading_gate().clear(BLOCKER[0])
        await client.aclose()


async def test_cancellation_stays_available_while_entries_are_blocked(
    db_engine, credentials, fake_clock
):
    """Reducing exposure must never be gated by the entry blocker."""
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        get_trading_gate().block(*BLOCKER)
        response = await client.post(
            f"/api/v1/orders/{identifier}/cancel",
            json={
                "request_id": str(uuid4()),
                "reason": "Owner closes the entry while trading is halted",
                "confirmation": "CANCEL PAPER ENTRY",
            },
        )
        assert response.status_code != 423, response.text
    finally:
        get_trading_gate().clear(BLOCKER[0])
        await client.aclose()


async def test_the_lock_cites_only_the_components_still_blocking(
    db_engine, credentials, fake_clock
):
    """Clearing a blocker stops it being cited, blocker by blocker.

    Submitting leaves the engine's own `paper_protection` blocker in force until
    the new position has been monitored, so the endpoint stays locked here for a
    reason of its own. That is correct behaviour, and it is why this asserts on
    which component is cited rather than on the lock disappearing entirely.
    """
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        get_trading_gate().block(*BLOCKER)
        locked = await client.post(
            f"/api/v1/orders/{identifier}/replace", json=replacement_body()
        )
        assert locked.status_code == 423
        assert BLOCKER[0] in locked.json()["detail"]
        get_trading_gate().clear(BLOCKER[0])
        cleared = await client.post(
            f"/api/v1/orders/{identifier}/replace", json=replacement_body()
        )
        assert BLOCKER[0] not in cleared.text
        assert "paper_protection" in cleared.text
    finally:
        get_trading_gate().clear(BLOCKER[0])
        await client.aclose()
