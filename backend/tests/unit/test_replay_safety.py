from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.clock import IST, FakeClock
from app.core.enums import Exchange, Segment
from app.core.errors import ValidationError
from app.marketdata.models import Bar, InstrumentRef
from app.marketdata.replay import ReplayMarketDataProvider

pytestmark = pytest.mark.unit
BASE = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
FIRST = InstrumentRef("FIRST", Exchange.NSE, Segment.CASH)
SECOND = InstrumentRef("SECOND", Exchange.NSE, Segment.CASH)


def candle(minutes=0, close="101"):
    return Bar(
        BASE + timedelta(minutes=minutes),
        Decimal("100"),
        Decimal("110"),
        Decimal("90"),
        Decimal(close),
        100,
    )


async def test_warmup_preserves_application_clock_and_withholds_future_close():
    clock = FakeClock(BASE + timedelta(minutes=1, seconds=30), monotonic_start=25)
    provider = ReplayMarketDataProvider(clock)
    provider.load(FIRST, 1, [candle(), candle(1, "105")])
    await provider.prime()
    assert provider.clock is clock
    assert clock.now() == BASE + timedelta(minutes=1, seconds=30)
    assert clock.monotonic() == 25
    assert (await provider.get_quote(FIRST)).ltp == Decimal(101)
    assert provider.next_event_at == BASE + timedelta(minutes=2)
    with pytest.raises(ValidationError, match="cannot change"):
        provider.load(SECOND, 1, [candle()])
    with pytest.raises(ValidationError, match="unused"):
        await provider.prime()
    assert await provider.step() == BASE + timedelta(minutes=2)
    assert (await provider.get_quote(FIRST)).ltp == Decimal(105)


async def test_warmup_failure_restores_clock_and_permanently_stops_provider():
    clock = FakeClock(BASE + timedelta(minutes=5))
    provider = ReplayMarketDataProvider(clock)
    provider.load(FIRST, 1, [candle(4)])
    provider.load(FIRST, 5, [candle(0, "105")])
    with pytest.raises(ValidationError, match="Conflicting"):
        await provider.prime()
    assert provider.clock is clock
    assert clock.now() == BASE + timedelta(minutes=5)
    with pytest.raises(ValidationError, match="failed"):
        await provider.step()
    with pytest.raises(ValidationError, match="cannot change"):
        provider.load(SECOND, 1, [candle()])


async def test_closure_controls_visibility_and_old_prices_are_not_refreshed():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(FIRST, 1, [candle()])
    provider.load(SECOND, 5, [candle(close="105")])
    assert await provider.step() == BASE + timedelta(minutes=1)
    assert await provider.get_candles(SECOND, 5, BASE, BASE + timedelta(days=1)) == []
    with pytest.raises(ValidationError):
        await provider.get_quote(SECOND)
    assert await provider.step() == BASE + timedelta(minutes=5)
    assert (await provider.get_quote(SECOND)).ltp == Decimal("105")
    first_close = BASE + timedelta(minutes=1)
    assert (await provider.get_quote(FIRST)).observed_at == first_close
    assert (await provider.get_ltp([FIRST]))[FIRST.key].observed_at == first_close
    assert (await provider.get_ohlc([FIRST]))[FIRST.key].observed_at == first_close
    assert await provider.last_update_at(FIRST) == first_close


async def test_all_simultaneous_series_publish_before_any_callback():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(SECOND, 1, [candle(close="102")])
    provider.load(FIRST, 1, [candle()])
    observed = []

    async def callback(quote):
        prices = await provider.get_ltp([FIRST, SECOND])
        assert set(prices) == {FIRST.key, SECOND.key}
        assert all(price.observed_at == BASE + timedelta(minutes=1) for price in prices.values())
        observed.append(quote.instrument.key)

    provider.on_tick(callback)
    await provider.step()
    assert observed == [FIRST.key, SECOND.key]


async def test_rewind_hides_future_values_and_refuses_continuation():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(FIRST, 1, [candle(), candle(1)])
    await provider.step()
    provider.clock.set_to(BASE)
    assert await provider.get_ltp([FIRST]) == {}
    assert await provider.get_ohlc([FIRST]) == {}
    assert await provider.last_update_at(FIRST) is None
    assert await provider.get_candles(FIRST, 1, BASE, BASE + timedelta(days=1)) == []
    with pytest.raises(ValidationError):
        await provider.get_quote(FIRST)
    with pytest.raises(ValidationError, match="backwards"):
        await provider.step()
    with pytest.raises(ValidationError, match="cannot change"):
        provider.load(FIRST, 1, [candle()])


@pytest.mark.parametrize(
    "interval,bars",
    [
        (0, [candle()]),
        (True, [candle()]),
        (1.5, [candle()]),
        (1, [replace(candle(), ts=BASE.replace(tzinfo=None))]),
        (1, [replace(candle(), close=Decimal("NaN"))]),
        (1, [replace(candle(), volume=-1)]),
        (1, [candle(), candle()]),
        (5, [candle(), candle(1)]),
    ],
)
def test_malformed_sources_are_rejected_before_replay(interval, bars):
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    with pytest.raises(ValidationError):
        provider.load(FIRST, interval, bars)
    assert provider.series == []


async def test_conflicting_interval_closes_do_not_partially_publish():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(FIRST, 5, [candle(close="105")])
    provider.load(FIRST, 1, [candle(4, close="106")])
    with pytest.raises(ValidationError, match="Conflicting"):
        await provider.step()
    assert provider.clock.now() == BASE
    assert all(series.cursor == 0 for series in provider.series)
    assert await provider.get_ltp([FIRST]) == {}


@pytest.mark.parametrize("reverse", [False, True])
async def test_consistent_intervals_emit_one_deterministic_quote(reverse):
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    inputs = [(5, candle(close="105")), (1, candle(4, close="105"))]
    for interval, bar in reversed(inputs) if reverse else inputs:
        provider.load(FIRST, interval, [bar])
    ticks = []

    async def callback(quote):
        ticks.append(quote)

    provider.on_tick(callback)
    await provider.step()
    assert len(ticks) == 1
    assert ticks[0].observed_at == BASE + timedelta(minutes=5)
    assert ticks[0].ltp == Decimal("105")
    assert all(series.cursor == 1 for series in provider.series)


async def test_callback_failure_prevents_silent_continuation():
    provider = ReplayMarketDataProvider(FakeClock(BASE))
    provider.load(FIRST, 1, [candle(), candle(1)])

    async def callback(_quote):
        raise RuntimeError("consumer failed")

    provider.on_tick(callback)
    with pytest.raises(RuntimeError, match="consumer failed"):
        await provider.step()
    with pytest.raises(ValidationError, match="failed"):
        await provider.step()
    assert provider.clock.now() == BASE + timedelta(minutes=1)
    assert provider.series[0].cursor == 1
