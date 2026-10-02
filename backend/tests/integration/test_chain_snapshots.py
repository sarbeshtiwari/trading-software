"""Synthetic snapshot persistence, immutability, cadence and historical visibility."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import UTC, FakeClock
from app.fno.chain.snapshots import ChainSnapshotStore
from tests.unit.test_option_chain import EXPIRY, OBSERVED, synthetic_chain


async def test_chain_snapshot_persistence(db_engine):
    clock = FakeClock(OBSERVED)
    store = ChainSnapshotStore(cadence=timedelta(minutes=1), clock=clock)
    chain = replace(synthetic_chain(), raw={"source": "isolated synthetic test"})
    assert await store.capture(chain)
    assert not await store.capture(chain)
    restored = await store.read("TEST", EXPIRY, as_of=OBSERVED.astimezone(UTC))
    assert restored == [chain]
    assert restored[0].strikes[0].call.ltp == Decimal(5)
    assert restored[0].data_origin == chain.data_origin
    assert not await store.read("OTHER", EXPIRY, as_of=OBSERVED)


async def test_snapshot_cadence_survives_restart(db_engine):
    clock = FakeClock(OBSERVED)
    store = ChainSnapshotStore(cadence=timedelta(minutes=1), clock=clock)
    assert await store.capture(synthetic_chain())
    clock.advance(timedelta(seconds=59))
    restarted = ChainSnapshotStore(cadence=timedelta(minutes=1), clock=clock)
    assert not await restarted.capture(replace(synthetic_chain(), observed_at=clock.now()))
    clock.advance(timedelta(seconds=1))
    assert await restarted.capture(replace(synthetic_chain(), observed_at=clock.now()))
    assert len(await store.read("TEST", EXPIRY, as_of=clock.now())) == 2


async def test_snapshot_never_overwrites_audited_observations(db_engine):
    store = ChainSnapshotStore(cadence=timedelta(seconds=1), clock=FakeClock(OBSERVED))
    assert await store.capture(synthetic_chain())
    with pytest.raises(ValueError, match="overwrite"):
        await store.capture(replace(synthetic_chain(), spot=Decimal(102)))
    assert (await store.read("TEST", EXPIRY, as_of=OBSERVED))[0].spot == 101


async def test_snapshot_no_lookahead_including_late_receipt(db_engine):
    clock = FakeClock(OBSERVED + timedelta(minutes=5))
    store = ChainSnapshotStore(cadence=timedelta(seconds=1), clock=clock)
    assert await store.capture(synthetic_chain())
    assert not await store.read("TEST", EXPIRY, as_of=OBSERVED - timedelta(seconds=1))
    assert not await store.read("TEST", EXPIRY, as_of=OBSERVED + timedelta(minutes=4))
    assert len(await store.read("TEST", EXPIRY, as_of=clock.now())) == 1
    with pytest.raises(ValueError, match="future"):
        await store.capture(
            replace(synthetic_chain(), observed_at=clock.now() + timedelta(seconds=1))
        )


async def test_snapshot_rejects_out_of_order(db_engine):
    clock = FakeClock(OBSERVED + timedelta(minutes=5))
    store = ChainSnapshotStore(cadence=timedelta(seconds=1), clock=clock)
    assert await store.capture(replace(synthetic_chain(), observed_at=clock.now()))
    with pytest.raises(ValueError, match="out-of-order"):
        await store.capture(synthetic_chain())


def test_snapshot_cadence_requires_positive_interval():
    with pytest.raises(ValueError):
        ChainSnapshotStore(cadence=timedelta(0))
