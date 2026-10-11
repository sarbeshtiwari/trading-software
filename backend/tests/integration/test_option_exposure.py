"""FNOR-001: a wired option position refuses to be valued on its premium.

The pending/filled agreement is asserted in `test_option_evidence.py`, where the
same four units are measured on the underlying in both paths. These tests cover
what happens when that underlying is not actually there: the measure must become
unavailable, because the only alternative reading — the premium — is roughly an
order of magnitude smaller than the exposure the contract carries.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.clock import UTC
from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.market_data import OptionChainSnapshot
from app.db.models.trading import Order, Position
from tests.integration.test_option_evidence import UNDERLYING_SPOT, setup_option_worker
from tests.integration.test_reference_worker import credentials

__all__ = ["credentials"]

STRATEGY = "long-option-breakout"


async def open_option_position(credentials, clock, tmp_path):
    """One real worker cycle, leaving four filled units of the fixture option."""
    worker, _provider, client, _chain = await setup_option_worker(credentials, clock, tmp_path)
    await worker.cycle()
    assert not worker.failed, worker.detail
    async with db_session.session_scope() as session:
        position = await session.scalar(sa.select(Position))
    assert position.net_quantity == 4
    return worker, client, position


async def test_entry_stands_down_when_the_chain_reports_no_underlying(
    db_engine, credentials, fake_clock, tmp_path
):
    """The pending side fails closed too: no underlying means no sized entry."""
    worker, provider, client, chain = await setup_option_worker(credentials, fake_clock, tmp_path)
    try:
        provider.get_option_chain.return_value = replace(chain, spot=None)
        await worker.cycle()
        assert worker.detail == "CONTRACT_DERIVATION_UNAVAILABLE"
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(Position)) == 0
    finally:
        await worker.stop()
        await client.aclose()


async def rewrite_snapshot_chain(**fields):
    """Change the stored chain the option producer captured, nothing else."""
    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(OptionChainSnapshot))
        assert row is not None, "the option producer must have captured a chain"
        row.strikes = {**row.strikes, "chain": {**row.strikes["chain"], **fields}}


async def refresh_mark(clock):
    """Keep the premium mark current so only the underlying is in question."""
    async with db_session.session_scope() as session:
        position = await session.scalar(sa.select(Position))
        position.marked_at = clock.utcnow()


async def test_filled_option_exposure_uses_the_observed_underlying(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, client, position = await open_option_position(credentials, fake_clock, tmp_path)
    try:
        account = await worker.executor.portfolio_state(STRATEGY, DataOrigin.SYNTHETIC)
        assert sum(item.notional for item in account.exposures) == 4 * UNDERLYING_SPOT
        # The premium mark is a tenth of that, and must not be what is measured.
        assert position.last_price == Decimal(100)
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("spot", [None, 0, "-1", "NaN"])
async def test_option_position_without_a_usable_underlying_fails_closed(
    db_engine, credentials, fake_clock, tmp_path, spot
):
    """An absent or nonsensical underlying is unavailable, never the premium."""
    worker, client, _position = await open_option_position(credentials, fake_clock, tmp_path)
    try:
        await rewrite_snapshot_chain(spot=spot)
        with pytest.raises(SafetyError, match="PAPER_POSITION_UNDERLYING_UNAVAILABLE"):
            await worker.executor.portfolio_state(STRATEGY, DataOrigin.SYNTHETIC)
    finally:
        await worker.stop()
        await client.aclose()


async def test_stale_underlying_cannot_value_an_option_position(
    db_engine, credentials, fake_clock, tmp_path
):
    """The underlying must meet the same freshness bar as the premium mark."""
    worker, client, _position = await open_option_position(credentials, fake_clock, tmp_path)
    try:
        # Measure from the timestamp actually stored, not from "now": the fixture
        # observed its chain before the cycle, so it is already partly aged.
        staleness = timedelta(seconds=worker.executor.settings.tick_staleness_seconds)
        async with db_session.session_scope() as session:
            observed = await session.scalar(sa.select(OptionChainSnapshot.ts))
        observed = observed if observed.tzinfo else observed.replace(tzinfo=UTC)
        fake_clock.advance(observed + staleness - fake_clock.utcnow())
        await refresh_mark(fake_clock)
        boundary = await worker.executor.portfolio_state(STRATEGY, DataOrigin.SYNTHETIC)
        assert sum(item.notional for item in boundary.exposures) == 4 * UNDERLYING_SPOT
        fake_clock.advance(timedelta(seconds=1))
        await refresh_mark(fake_clock)
        with pytest.raises(SafetyError, match="PAPER_POSITION_UNDERLYING_UNAVAILABLE"):
            await worker.executor.portfolio_state(STRATEGY, DataOrigin.SYNTHETIC)
    finally:
        await worker.stop()
        await client.aclose()


async def test_underlying_from_another_origin_is_not_borrowed(
    db_engine, credentials, fake_clock, tmp_path
):
    """A chain observed under a different origin cannot price this position."""
    worker, client, _position = await open_option_position(credentials, fake_clock, tmp_path)
    try:
        await rewrite_snapshot_chain(data_origin=DataOrigin.HISTORICAL.value)
        with pytest.raises(SafetyError, match="PAPER_POSITION_UNDERLYING_UNAVAILABLE"):
            await worker.executor.portfolio_state(STRATEGY, DataOrigin.SYNTHETIC)
    finally:
        await worker.stop()
        await client.aclose()
