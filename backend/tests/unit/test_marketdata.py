"""Market-data layer: staleness, validation, cache, aggregation, subscriptions,
feed budget and reconnection, replay.

Covers MD-003, MD-004, MD-006, MD-008, MD-009, MD-010, HD-008, GRW-022, GRW-023.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import pytest

from app.brokers.groww.feed import (
    FeedPriority,
    GrowwFeedClient,
    Subscription,
    SubscriptionBudget,
)
from app.core.clock import IST, FakeClock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.core.errors import StaleDataError
from app.core.events import EventType, MemoryEventBus
from app.marketdata.aggregator import CandleAggregator, bar_start
from app.marketdata.backfill import expected_timestamps, find_gaps
from app.marketdata.cache import MemoryQuoteCache
from app.marketdata.models import Bar, DepthLevel, InstrumentRef, LTPQuote, Quote
from app.marketdata.replay import ReplayMarketDataProvider
from app.marketdata.staleness import (
    FreshnessPolicy,
    bar_is_stale,
    check_fresh,
    evaluate,
    is_fresh,
)
from app.marketdata.subscriptions import Interest, SubscriptionManager
from app.marketdata.validation import Anomaly, validate_bar, validate_quote, validate_series

pytestmark = pytest.mark.unit

RELIANCE = InstrumentRef("RELIANCE", Exchange.NSE, Segment.CASH)
TCS = InstrumentRef("TCS", Exchange.NSE, Segment.CASH)
NIFTY_FUT = InstrumentRef("NIFTY26SEPFUT", Exchange.NSE, Segment.FNO)


def _quote(**overrides: Any) -> Quote:
    from app.core.clock import get_clock

    defaults: dict[str, Any] = dict(
        instrument=RELIANCE,
        ltp=Decimal("1400"),
        observed_at=get_clock().now(),
        data_origin=DataOrigin.LIVE,
    )
    defaults.update(overrides)
    return Quote(**defaults)


def _bar(ts: datetime, o: str, h: str, l: str, c: str, volume: int = 1000) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=volume,
    )


# --- MD-008 ---------------------------------------------------------------


def test_stale_data_blocks_entry(fake_clock: FakeClock) -> None:
    policy = FreshnessPolicy(tick_seconds=15)
    observed = fake_clock.now()

    assert is_fresh(observed, kind="tick", policy=policy, clock=fake_clock)
    check_fresh(observed, kind="tick", policy=policy, clock=fake_clock)

    fake_clock.advance_seconds(16)
    assert not is_fresh(observed, kind="tick", policy=policy, clock=fake_clock)

    with pytest.raises(StaleDataError) as excinfo:
        check_fresh(
            observed, kind="tick", what="NIFTY price", policy=policy, clock=fake_clock
        )
    assert "stale" in str(excinfo.value)
    assert excinfo.value.context["age_seconds"] == pytest.approx(16.0)


def test_missing_timestamp_counts_as_stale(fake_clock: FakeClock) -> None:
    """No timestamp means unknown age, and unknown age is not actionable."""
    report = evaluate(None, kind="tick", clock=fake_clock)
    assert report.stale
    assert "no observation timestamp" in report.detail


def test_future_timestamps_are_distrusted(fake_clock: FakeClock) -> None:
    future = fake_clock.now() + timedelta(seconds=30)
    report = evaluate(future, kind="tick", clock=fake_clock)
    assert report.stale
    assert "future" in report.detail


def test_greeks_get_their_own_freshness_budget(fake_clock: FakeClock) -> None:
    policy = FreshnessPolicy(tick_seconds=15, greeks_seconds=60)
    observed = fake_clock.now()
    fake_clock.advance_seconds(30)

    assert not is_fresh(observed, kind="tick", policy=policy, clock=fake_clock)
    assert is_fresh(observed, kind="greeks", policy=policy, clock=fake_clock)


def test_bar_staleness_allows_a_grace_interval(fake_clock: FakeClock) -> None:
    bar_ts = fake_clock.now()
    fake_clock.advance(timedelta(minutes=6))
    # A 5m bar closes 5m after its open; two intervals of grace.
    assert not bar_is_stale(bar_ts, 5, clock=fake_clock)
    fake_clock.advance(timedelta(minutes=12))
    assert bar_is_stale(bar_ts, 5, clock=fake_clock)


# --- MD-009 ---------------------------------------------------------------


def test_data_quality_rules() -> None:
    assert validate_quote(_quote()).ok

    assert Anomaly.NON_POSITIVE_PRICE in validate_quote(_quote(ltp=Decimal("0"))).anomalies

    crossed = _quote(
        bids=(DepthLevel(price=Decimal("1401"), quantity=10),),
        asks=(DepthLevel(price=Decimal("1400"), quantity=10),),
    )
    result = validate_quote(crossed)
    assert Anomaly.CROSSED_BOOK in result.anomalies
    assert not result.ok

    outside = _quote(ltp=Decimal("1600"), upper_circuit=Decimal("1500"))
    assert Anomaly.OUTSIDE_CIRCUIT in validate_quote(outside).anomalies
    assert not validate_quote(outside).ok

    below = _quote(ltp=Decimal("1000"), lower_circuit=Decimal("1200"))
    assert Anomaly.OUTSIDE_CIRCUIT in validate_quote(below).anomalies


def test_implausible_jump_is_a_warning_not_a_rejection() -> None:
    """Real gaps happen; refusing to see them would be its own failure."""
    result = validate_quote(_quote(ltp=Decimal("1800")), previous_price=Decimal("1400"))
    assert Anomaly.IMPLAUSIBLE_JUMP in result.anomalies
    assert result.ok  # usable, but flagged
    assert Anomaly.IMPLAUSIBLE_JUMP in result.warnings


def test_wide_spread_is_flagged() -> None:
    wide = _quote(
        bids=(DepthLevel(price=Decimal("1380"), quantity=10),),
        asks=(DepthLevel(price=Decimal("1420"), quantity=10),),
    )
    assert Anomaly.WIDE_SPREAD in validate_quote(wide).anomalies


def test_bar_validation_catches_inconsistent_candles(fake_clock: FakeClock) -> None:
    ts = fake_clock.now()
    good = _bar(ts, "100", "105", "99", "104")
    assert validate_bar(good).ok

    inconsistent = Bar(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("95"),
        low=Decimal("105"),
        close=Decimal("100"),
        volume=10,
    )
    result = validate_bar(inconsistent)
    assert Anomaly.INCONSISTENT_OHLC in result.anomalies
    assert not result.ok

    # Price cannot move without trades.
    zero_volume = _bar(ts, "100", "105", "99", "104", volume=0)
    assert Anomaly.ZERO_VOLUME_WITH_MOVE in validate_bar(zero_volume).anomalies


def test_validate_series_filters_and_counts(fake_clock: FakeClock) -> None:
    ts = fake_clock.now()
    bars = [
        _bar(ts, "100", "105", "99", "104"),
        Bar(
            ts=ts + timedelta(minutes=5),
            open=Decimal("104"),
            high=Decimal("100"),
            low=Decimal("110"),
            close=Decimal("105"),
            volume=10,
        ),
        _bar(ts + timedelta(minutes=10), "105", "108", "104", "107"),
    ]
    clean, counters = validate_series(bars)
    assert len(clean) == 2
    assert counters.rejected == 1
    assert counters.by_anomaly[Anomaly.INCONSISTENT_OHLC.value] == 1


# --- MD-003 ---------------------------------------------------------------


async def test_tick_cache(fake_clock: FakeClock) -> None:
    cache = MemoryQuoteCache(clock=fake_clock)
    quote = LTPQuote(
        instrument=RELIANCE, ltp=Decimal("1400"), observed_at=fake_clock.now()
    )

    assert await cache.get(RELIANCE.key) is None
    await cache.set(quote, ttl_seconds=1.0)

    hit = await cache.get(RELIANCE.key)
    assert hit is not None
    assert hit.ltp == Decimal("1400")
    assert cache.stats.hits == 1

    # Expiry is a miss, not a stale hit.
    fake_clock.advance_seconds(1.1)
    assert await cache.get(RELIANCE.key) is None
    assert cache.stats.misses == 2


# --- MD-006 ---------------------------------------------------------------


def test_bar_start_anchors_to_the_session_open() -> None:
    # 09:17 belongs to the 09:15-09:20 bar, not to 09:15-09:20 by luck.
    assert bar_start(datetime(2026, 1, 5, 9, 17, tzinfo=IST), 5) == datetime(
        2026, 1, 5, 9, 15, tzinfo=IST
    )
    assert bar_start(datetime(2026, 1, 5, 9, 20, tzinfo=IST), 5) == datetime(
        2026, 1, 5, 9, 20, tzinfo=IST
    )
    assert bar_start(datetime(2026, 1, 5, 10, 3, tzinfo=IST), 15) == datetime(
        2026, 1, 5, 10, 0, tzinfo=IST
    )
    # 60m bars anchored at 09:15 run 09:15-10:15.
    assert bar_start(datetime(2026, 1, 5, 10, 10, tzinfo=IST), 60) == datetime(
        2026, 1, 5, 9, 15, tzinfo=IST
    )
    # Pre-open ticks align backwards from the anchor.
    assert bar_start(datetime(2026, 1, 5, 9, 12, tzinfo=IST), 5) == datetime(
        2026, 1, 5, 9, 10, tzinfo=IST
    )


def test_candle_aggregation() -> None:
    closed_bars: list[tuple[int, Bar]] = []
    aggregator = CandleAggregator(
        intervals=(1, 5), on_bar_closed=lambda _i, interval, bar: closed_bars.append((interval, bar))
    )
    base = datetime(2026, 1, 5, 9, 15, tzinfo=IST)

    ticks = [
        (base + timedelta(seconds=10), "100.00", 100),
        (base + timedelta(seconds=30), "101.50", 250),
        (base + timedelta(seconds=50), "99.50", 400),
        (base + timedelta(minutes=1, seconds=5), "100.25", 500),
    ]
    closed: list[tuple[int, Bar]] = []
    for ts, price, volume in ticks:
        closed.extend(
            aggregator.on_tick(RELIANCE, Decimal(price), ts, cumulative_volume=volume)
        )

    # The fourth tick closes the first 1-minute bar.
    one_minute = [bar for interval, bar in closed if interval == 1]
    assert len(one_minute) == 1
    bar = one_minute[0]
    assert bar.ts == base
    assert bar.open == Decimal("100.00")
    assert bar.high == Decimal("101.50")
    assert bar.low == Decimal("99.50")
    assert bar.close == Decimal("99.50")
    assert bar.volume == 400  # cumulative 400 minus the 0 at start

    # The 5-minute bar is still open.
    assert not [b for i, b in closed if i == 5]
    current = aggregator.current(RELIANCE, 5)
    assert current is not None
    assert current.close == Decimal("100.25")


def test_aggregator_ignores_out_of_order_ticks() -> None:
    aggregator = CandleAggregator(intervals=(1,))
    base = datetime(2026, 1, 5, 9, 20, tzinfo=IST)

    aggregator.on_tick(RELIANCE, Decimal("100"), base)
    aggregator.on_tick(RELIANCE, Decimal("101"), base + timedelta(seconds=30))
    # A tick from the previous bar arrives late and must not alter this one.
    aggregator.on_tick(RELIANCE, Decimal("90"), base - timedelta(minutes=2))

    current = aggregator.current(RELIANCE, 1)
    assert current is not None
    assert current.low == Decimal("100")


def test_aggregator_flush_closes_open_bars() -> None:
    aggregator = CandleAggregator(intervals=(1, 5))
    aggregator.on_tick(RELIANCE, Decimal("100"), datetime(2026, 1, 5, 9, 16, tzinfo=IST))

    flushed = aggregator.flush()
    assert {interval for interval, _ in flushed} == {1, 5}
    assert aggregator.current(RELIANCE, 1) is None


# --- GRW-022: subscription budget ----------------------------------------


def test_feed_subscription_cap() -> None:
    budget = SubscriptionBudget(limit=3)
    watches = [
        Subscription(InstrumentRef(f"SYM{i}", Exchange.NSE, Segment.CASH), FeedPriority.WATCH)
        for i in range(3)
    ]
    for subscription in watches:
        assert budget.add(subscription)[0]
    assert len(budget) == 3

    # A position matters more than a watchlist entry, so something gets evicted.
    position = Subscription(
        InstrumentRef("HELD", Exchange.NSE, Segment.CASH), FeedPriority.POSITION
    )
    added, evicted = budget.add(position)
    assert added
    assert evicted is not None
    assert evicted.priority is FeedPriority.WATCH
    assert len(budget) == 3

    # Another watch cannot displace anything now.
    extra = Subscription(
        InstrumentRef("EXTRA", Exchange.NSE, Segment.CASH), FeedPriority.WATCH
    )
    added, evicted = budget.add(extra)
    assert not added
    assert evicted is None


def test_budget_upgrades_priority_without_duplicating() -> None:
    budget = SubscriptionBudget(limit=5)
    watch = Subscription(RELIANCE, FeedPriority.WATCH)
    position = Subscription(RELIANCE, FeedPriority.POSITION)

    budget.add(watch)
    budget.add(position)
    assert len(budget) == 1
    assert budget.current()[0].priority is FeedPriority.POSITION


# --- MD-004 ---------------------------------------------------------------


class _FakeFeed:
    """Stands in for GrowwFeedClient, recording what it was asked to do."""

    def __init__(self, limit: int = 1000) -> None:
        self.budget = SubscriptionBudget(limit=limit)
        self.subscribed: list[Subscription] = []
        self.unsubscribed: list[Subscription] = []

    async def subscribe(self, subscriptions):  # type: ignore[no-untyped-def]
        accepted = []
        for subscription in subscriptions:
            added, _ = self.budget.add(subscription)
            if added:
                accepted.append(subscription)
                self.subscribed.append(subscription)
        return accepted

    async def unsubscribe(self, subscriptions):  # type: ignore[no-untyped-def]
        for subscription in subscriptions:
            self.budget.remove(subscription)
            self.unsubscribed.append(subscription)


async def test_subscription_manager() -> None:
    feed = _FakeFeed()
    manager = SubscriptionManager(feed=feed)  # type: ignore[arg-type]

    await manager.register([Interest(RELIANCE, "strategy"), Interest(TCS, "watchlist")])
    assert len(manager) == 2
    assert len(feed.subscribed) == 2

    # A second interested party does not re-subscribe.
    await manager.register([Interest(RELIANCE, "position")])
    assert len(feed.subscribed) == 3  # priority upgrade re-subscribes
    assert len(manager) == 2

    # Releasing one holder keeps the subscription while another wants it.
    await manager.release([Interest(RELIANCE, "strategy")])
    assert len(manager) == 2
    assert not feed.unsubscribed

    await manager.release([Interest(RELIANCE, "position")])
    assert len(manager) == 1
    assert len(feed.unsubscribed) == 1


async def test_release_source_drops_everything_that_source_wanted() -> None:
    feed = _FakeFeed()
    manager = SubscriptionManager(feed=feed)  # type: ignore[arg-type]
    await manager.register(
        [Interest(RELIANCE, "strategy"), Interest(TCS, "strategy"), Interest(NIFTY_FUT, "position")]
    )

    await manager.release_source("strategy")
    remaining = {ref.key for ref in manager.subscribed}
    assert remaining == {NIFTY_FUT.key}


# --- GRW-023: reconnection ------------------------------------------------


class _FlakyTransport:
    """Fails once, then delivers messages."""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.connects = 0
        self.subscribed: list[Subscription] = []
        self._messages: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        return None

    async def subscribe(self, subscriptions) -> None:  # type: ignore[no-untyped-def]
        self.subscribed.extend(subscriptions)

    async def unsubscribe(self, subscriptions) -> None:  # type: ignore[no-untyped-def]
        return None

    async def next_message(self):  # type: ignore[no-untyped-def]
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("socket closed")
        return await self._messages.get()

    def push(self, message: dict) -> None:
        self._messages.put_nowait(message)


async def test_feed_reconnect(fake_clock: FakeClock) -> None:
    transport = _FlakyTransport(fail_times=1)
    bus = MemoryEventBus()
    await bus.start()

    gaps: list[dict] = []

    async def collect(event):  # type: ignore[no-untyped-def]
        gaps.append(event.payload)

    bus.subscribe(EventType.FEED_GAP, collect, name="collector")

    client = GrowwFeedClient(
        transport,  # type: ignore[arg-type]
        event_bus=bus,
        clock=fake_clock,
        base_backoff_seconds=0.001,
    )
    received: list[dict] = []
    client.on_message(lambda message: _record(received, message))

    await client.subscribe([Subscription(RELIANCE, FeedPriority.POSITION)])
    await client.start()

    # Let the failure, backoff and reconnect happen.
    for _ in range(50):
        await asyncio.sleep(0)
        if client.stats.reconnects:
            break
        await asyncio.sleep(0.005)

    transport.push({"message": {"trading_symbol": "RELIANCE", "ltp": 1400}})
    for _ in range(50):
        await asyncio.sleep(0)
        if received:
            break
        await asyncio.sleep(0.005)

    await client.stop()

    assert client.stats.reconnects == 1
    assert transport.connects == 2
    # The previous subscription set was restored, not lost.
    assert len([s for s in transport.subscribed if s.instrument.key == RELIANCE.key]) == 2
    # And the outage was published so consumers know their data has a hole.
    assert gaps and "gap_seconds" in gaps[0]
    assert received


async def _record(sink: list, message: dict) -> None:
    sink.append(message)


# --- HD-008 ---------------------------------------------------------------


async def test_replay_no_lookahead() -> None:
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 15, tzinfo=IST))
    provider = ReplayMarketDataProvider(clock=clock)

    base = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    bars = [
        _bar(base + timedelta(minutes=5 * i), "100", "105", "99", str(100 + i))
        for i in range(5)
    ]
    provider.load(RELIANCE, 5, bars)

    assert provider.data_origin is DataOrigin.REPLAY

    # Nothing is visible before the first step.
    assert await provider.get_candles(RELIANCE, 5, base, base + timedelta(hours=2)) == []

    await provider.step()
    visible = await provider.get_candles(RELIANCE, 5, base, base + timedelta(hours=2))
    assert len(visible) == 1
    assert clock.now() == base + timedelta(minutes=5)

    await provider.step()
    visible = await provider.get_candles(RELIANCE, 5, base, base + timedelta(hours=2))
    assert len(visible) == 2
    # The clock advanced with the replay, so time-gated logic behaves as it did.
    assert clock.now() == base + timedelta(minutes=10)

    # Future bars remain invisible no matter how wide the requested window.
    far_future = await provider.get_candles(RELIANCE, 5, base, base + timedelta(days=30))
    assert len(far_future) == 2

    steps = await provider.run()
    assert steps == 3
    assert provider.exhausted


async def test_replay_ticks_reach_subscribers() -> None:
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 15, tzinfo=IST))
    provider = ReplayMarketDataProvider(clock=clock)
    base = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    provider.load(RELIANCE, 5, [_bar(base, "100", "105", "99", "104")])

    ticks: list[LTPQuote] = []

    async def on_tick(quote: LTPQuote) -> None:
        ticks.append(quote)

    provider.on_tick(on_tick)
    await provider.step()

    assert len(ticks) == 1
    assert ticks[0].ltp == Decimal("104")
    assert ticks[0].data_origin is DataOrigin.REPLAY


async def test_replay_steps_multiple_instruments_in_time_order() -> None:
    clock = FakeClock(start=datetime(2026, 1, 5, 9, 15, tzinfo=IST))
    provider = ReplayMarketDataProvider(clock=clock)
    base = datetime(2026, 1, 5, 9, 15, tzinfo=IST)

    provider.load(RELIANCE, 5, [_bar(base, "100", "101", "99", "100")])
    provider.load(TCS, 5, [_bar(base, "200", "201", "199", "200"),
                           _bar(base + timedelta(minutes=5), "200", "202", "199", "201")])

    await provider.step()
    prices = await provider.get_ltp([RELIANCE, TCS])
    assert set(prices) == {RELIANCE.key, TCS.key}

    await provider.step()
    assert clock.now() == base + timedelta(minutes=10)


# --- HD-003 / HD-004 ------------------------------------------------------


def test_expected_timestamps_respect_the_session() -> None:
    from app.core.calendar import Holiday, TradingCalendar

    calendar = TradingCalendar(
        holidays=[Holiday(day=datetime(2026, 1, 26).date(), name="Republic Day")],
        complete_years=[2026],
    )
    start = datetime(2026, 1, 5, 0, 0, tzinfo=IST)
    end = datetime(2026, 1, 5, 23, 59, tzinfo=IST)

    stamps = expected_timestamps(start, end, 60, calendar=calendar)
    # 09:15 to 15:30 is 6.25 hours: seven hourly bars, the last one partial.
    assert stamps[0] == datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    assert stamps[-1] == datetime(2026, 1, 5, 15, 15, tzinfo=IST)
    assert len(stamps) == 7


def test_gaps_respect_calendar() -> None:
    """A weekend is not a gap, and chasing it would never succeed."""
    from app.core.calendar import TradingCalendar

    calendar = TradingCalendar(complete_years=[2026])
    friday = datetime(2026, 1, 9, 0, 0, tzinfo=IST)
    monday_end = datetime(2026, 1, 12, 23, 59, tzinfo=IST)

    stamps = expected_timestamps(friday, monday_end, 60, calendar=calendar)
    days = {ts.date() for ts in stamps}
    assert days == {friday.date(), monday_end.date()}

    report = find_gaps(
        set(stamps), friday, monday_end, 60, instrument="NSE_CASH_X", calendar=calendar
    )
    assert report.gap_count == 0
    assert report.completeness == 1.0


def test_gap_report_collapses_missing_bars_into_ranges() -> None:
    from app.core.calendar import TradingCalendar

    calendar = TradingCalendar(complete_years=[2026])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    end = datetime(2026, 1, 5, 15, 30, tzinfo=IST)

    stamps = expected_timestamps(start, end, 60, calendar=calendar)
    present = set(stamps[:2]) | set(stamps[5:])

    report = find_gaps(present, start, end, 60, calendar=calendar)
    assert report.gap_count == 3
    ranges = report.ranges()
    # Three consecutive missing hourly bars collapse into one fetchable range.
    assert len(ranges) == 1
    assert ranges[0][0] == stamps[2]
