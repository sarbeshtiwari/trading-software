"""Isolated recorded fixtures, not fabricated production market data."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from app.core.errors import ValidationError
from app.marketdata.models import DepthLevel, OHLCQuote, OptionChain, Quote
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.replay import ReplayMarketDataProvider
from tests.unit.test_replay_safety import BASE, FIRST, candle


def snapshots():
    observed = BASE + timedelta(seconds=50)
    available = BASE + timedelta(minutes=1)
    quote = Quote(
        FIRST,
        Decimal(101),
        observed,
        data_origin=DataOrigin.SYNTHETIC,
        bids=(DepthLevel(Decimal(100), 12),),
        asks=(DepthLevel(Decimal(102), 14),),
        raw={"fixture": ["original"]},
    )
    ohlc = OHLCQuote(
        FIRST,
        Decimal(100),
        Decimal(103),
        Decimal(99),
        Decimal(101),
        observed,
        previous_close=Decimal(98),
        data_origin=DataOrigin.SYNTHETIC,
    )
    chain = OptionChain(
        "FIXTURE", available.date(), observed, spot=Decimal(101), data_origin=DataOrigin.SYNTHETIC
    )
    return [
        RecordedSnapshot("isolated-fixture", available, value) for value in (quote, ohlc, chain)
    ]


async def test_snapshot_only_timeline_publishes_atomically_without_lookahead():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    records = snapshots()
    provider.load_snapshots(records)
    with pytest.raises(ValidationError):
        await provider.get_quote(FIRST)
    assert await provider.get_ohlc([FIRST]) == {}
    with pytest.raises(ValidationError):
        await provider.get_option_chain("FIXTURE", BASE.date())
    seen = []

    async def callback(tick):
        quote = await provider.get_quote(FIRST)
        assert quote.best_bid == 100 and quote.bids[0].quantity == 12
        assert (await provider.get_ohlc([FIRST]))[FIRST.key].previous_close == 98
        assert (await provider.get_option_chain("FIXTURE", BASE.date())).spot == 101
        seen.append(tick)

    provider.on_tick(callback)
    assert await provider.step() == records[0].available_at
    assert len(seen) == 1 and seen[0].observed_at == records[0].value.observed_at
    assert seen[0].data_origin == DataOrigin.REPLAY
    assert provider.exhausted and await provider.step() is None
    provider.clock.advance(timedelta(seconds=30))
    assert (await provider.get_quote(FIRST)).observed_at == records[0].value.observed_at
    assert await provider.last_update_at(FIRST) == records[0].value.observed_at
    with pytest.raises(ValidationError, match="cannot change"):
        provider.load_snapshots([])
    provider.clock.set_to(BASE)
    assert await provider.get_ltp([FIRST]) == {}
    assert await provider.get_ohlc([FIRST]) == {}
    assert await provider.last_update_at(FIRST) is None
    with pytest.raises(ValidationError):
        await provider.get_quote(FIRST)


async def test_declared_recorded_quotes_never_fall_back_to_newer_candle_prices():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(FIRST, 1, [candle(), candle(1, close="109")])
    provider.load_snapshots(snapshots())
    seen = []

    async def callback(tick):
        seen.append(tick.ltp)

    provider.on_tick(callback)
    await provider.run()
    assert seen == [Decimal(101)]
    assert (await provider.get_quote(FIRST)).ltp == 101
    assert (await provider.get_candles(FIRST, 1, BASE, provider.clock.now()))[-1].close == 109
    assert (await provider.get_ltp([FIRST]))[FIRST.key].observed_at == BASE + timedelta(seconds=50)


async def test_recordings_are_copied_and_failed_load_is_atomic():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    records = snapshots()
    provider.load_snapshots(records)
    records[0].value.raw["fixture"].append("mutated")
    with pytest.raises(ValidationError, match="Duplicate"):
        provider.load_snapshots(records)
    await provider.step()
    quote = await provider.get_quote(FIRST)
    assert quote.raw["fixture"] == ["original"]
    quote.raw["fixture"].append("returned mutation")
    assert (await provider.get_quote(FIRST)).raw["fixture"] == ["original"]


@pytest.mark.parametrize("invalid", ["future", "naive", "crossed", "regression"])
def test_invalid_recorded_sources_are_rejected_before_publication(invalid):
    record = snapshots()[0]
    if invalid == "future":
        records = [replace(record, available_at=BASE)]
    elif invalid == "naive":
        records = [replace(record, available_at=record.available_at.replace(tzinfo=None))]
    elif invalid == "crossed":
        records = [replace(record, value=replace(record.value, asks=(DepthLevel(Decimal(99), 1),)))]
    else:
        records = [
            record,
            replace(
                record,
                available_at=record.available_at + timedelta(seconds=1),
                value=replace(record.value, observed_at=BASE),
            ),
        ]
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    with pytest.raises(ValidationError):
        provider.load_snapshots(records)
    assert provider.exhausted
