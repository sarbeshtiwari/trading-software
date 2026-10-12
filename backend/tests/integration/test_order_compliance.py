"""EXCH-004: the grid check runs before any provider is called.

Each case leaves the order exactly as the risk engine approved it and instead
makes the instrument master disagree with it — a revised tick size or lot size,
which is how this goes wrong in practice. The order must then never reach the
broker at all, rather than being sent and rejected by the venue.
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.execution.compliance import ExchangeComplianceError
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution

__all__ = ["credentials"]


async def revise_contract(instrument_id, **fields):
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, instrument_id)
        for name, value in fields.items():
            setattr(instrument, name, value)
        return instrument.trading_symbol


async def test_off_tick_price_never_reaches_the_broker(db_engine, credentials, fake_clock):
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            instrument = await session.scalar(sa.select(Instrument))
        # 100.00 is not a whole number of 0.07 ticks.
        await revise_contract(instrument.id, tick_size=Decimal("0.07"))
        with pytest.raises(ExchangeComplianceError) as raised:
            await engine.submit(proposal)
        assert raised.value.code == "EXCH_PRICE_OFF_TICK"
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_compliant_order_still_reaches_the_broker(db_engine, credentials, fake_clock):
    """The guard must not block the ordinary path it is protecting."""
    engine, proposal, _market, client, _context = await setup_execution(credentials, fake_clock)
    try:
        identifier = await engine.submit(proposal)
        assert identifier
        assert len(await engine.broker.list_orders()) == 1
    finally:
        await client.aclose()
