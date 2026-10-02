"""Synthetic historical membership revisions and receipt-time visibility."""

from datetime import timedelta

import pytest

from app.analysis.equity_store import EquityStore
from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from tests.unit.test_equity import snapshot, universe_policy
from tests.unit.test_option_chain import OBSERVED


async def test_stored_universe_resolution(db_engine):
    clock = FakeClock(OBSERVED)
    store = EquityStore(clock)
    first = snapshot()
    record_id = await store.write(first)
    assert await store.write(first) == record_id
    initial = await store.universe(
        as_of=OBSERVED,
        origin=DataOrigin.SYNTHETIC,
        quantities={first.key: 1},
        policy=universe_policy(),
    )
    assert initial.included == (first.key,)
    clock.advance(timedelta(seconds=1))
    await store.write(snapshot(known_at=clock.now(), indices=()))
    assert (await store.at(OBSERVED, DataOrigin.SYNTHETIC)) == [first]
    changed = await store.universe(
        as_of=clock.now(),
        origin=DataOrigin.SYNTHETIC,
        quantities={first.key: 1},
        policy=universe_policy(),
    )
    assert changed.excluded[first.key] == ("INDEX_MEMBERSHIP",)
    assert await store.at(clock.now(), DataOrigin.LIVE) == []


async def test_equity_receipt_time_and_conflicts(db_engine):
    clock = FakeClock(OBSERVED + timedelta(seconds=5))
    store = EquityStore(clock)
    await store.write(snapshot())
    assert await store.at(OBSERVED, DataOrigin.SYNTHETIC) == []
    assert len(await store.at(clock.now(), DataOrigin.SYNTHETIC)) == 1
    with pytest.raises(ValueError, match="conflicting"):
        await store.write(snapshot(sector="DIFFERENT"))
    with pytest.raises(ValueError, match="future"):
        await store.write(snapshot(known_at=clock.now() + timedelta(seconds=1)))
