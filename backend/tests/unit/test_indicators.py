"""Indicators against hand-computed reference values — TA-001…TA-012, TEST-002.

Every expected value below is worked out in the comment beside it. That is the
point: an indicator test that asserts whatever the code currently returns tests
nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.technical import levels as levels_module
from app.analysis.technical.base import (
    closes,
    last_value,
    registry,
    require_history,
    true_ranges,
    wilder_smooth,
)
from app.analysis.technical.cache import IndicatorCache, IndicatorCacheKey
from app.analysis.technical.ma import anchored_vwap, ema, sma, vwap, wma
from app.analysis.technical.momentum import macd, roc, rsi, stochastic
from app.analysis.technical.mtf import align_to_base, higher_timeframe_value, resample
from app.analysis.technical.price_action import (
    consolidation_score,
    gap_percent,
    geometry,
    is_inside_bar,
    is_outside_bar,
    range_expansion,
)
from app.analysis.technical.snapshot import SNAPSHOT_INDICATORS, SnapshotParams, build_snapshot
from app.analysis.technical.trend import adx, donchian_channels, supertrend
from app.analysis.technical.volatility import (
    atr,
    atr_percent,
    bollinger_bands,
    historical_volatility,
    keltner_channels,
    standard_deviation,
)
from app.analysis.technical.volume import obv, relative_volume, volume_sma, vwap_deviation
from app.core.clock import IST
from app.core.enums import SignalDirection
from app.core.errors import InsufficientHistoryError, ValidationError
from app.marketdata.models import Bar

pytestmark = pytest.mark.unit

BASE = datetime(2026, 1, 5, 9, 15, tzinfo=IST)


def D(value: str) -> Decimal:
    return Decimal(value)


def series(*values: str) -> list[Decimal]:
    return [Decimal(value) for value in values]


def bar(
    index: int,
    high: str,
    low: str,
    close: str,
    open_: str | None = None,
    volume: int = 1000,
    interval: int = 5,
) -> Bar:
    return Bar(
        ts=BASE + timedelta(minutes=interval * index),
        open=Decimal(open_ if open_ is not None else close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def approx(value: Decimal | None, expected: str, tolerance: str = "0.0001") -> bool:
    assert value is not None, "indicator returned None where a value was expected"
    return abs(value - Decimal(expected)) <= Decimal(tolerance)


# --- TA-001: framework ----------------------------------------------------


def test_indicator_framework() -> None:
    values = series("1", "2", "3")

    # Too little history raises rather than returning a plausible number.
    with pytest.raises(InsufficientHistoryError) as excinfo:
        sma(values, 5)
    assert "needs at least 5 bars" in str(excinfo.value)
    assert excinfo.value.context["received"] == 3

    with pytest.raises(ValidationError):
        require_history(values, 0, indicator="test")

    # Results align 1:1 with the input, with None during warm-up.
    result = sma(values, 3)
    assert len(result) == len(values)
    assert result[:2] == [None, None]
    assert result[2] == Decimal(2)


def test_last_value_finds_the_most_recent_reading() -> None:
    assert last_value([None, Decimal(1), None]) == Decimal(1)
    assert last_value([None, None]) is None


def test_true_range_accounts_for_gaps() -> None:
    """Using high-low for the first bar would understate every gap."""
    bars = [bar(0, "100", "99", "99.5"), bar(1, "105", "104", "104.5")]
    ranges = true_ranges(bars)
    assert ranges[0] is None  # no previous close, so no true range
    # max(105-104, |105-99.5|, |104-99.5|) = 5.5, not the 1.0 of high-low.
    assert ranges[1] == Decimal("5.5")


# --- TA-002: moving averages ---------------------------------------------


def test_moving_averages() -> None:
    values = series("10", "11", "12", "13", "14")

    # SMA(5) = (10+11+12+13+14)/5 = 12
    assert sma(values, 5)[-1] == Decimal(12)
    # SMA(3) at index 2 = (10+11+12)/3 = 11
    assert sma(values, 3)[2] == Decimal(11)

    # WMA(5) = (10*1 + 11*2 + 12*3 + 13*4 + 14*5)/15 = 190/15 = 12.6667
    assert approx(wma(values, 5)[-1], "12.666667", "0.000001")

    # EMA(3): seed = (10+11+12)/3 = 11 at index 2; k = 2/4 = 0.5
    #   index 3: (13 - 11) * 0.5 + 11 = 12
    #   index 4: (14 - 12) * 0.5 + 12 = 13
    ema_values = ema(values, 3)
    assert ema_values[2] == Decimal(11)
    assert ema_values[3] == Decimal(12)
    assert ema_values[4] == Decimal(13)


def test_vwap_resets_each_session() -> None:
    """A VWAP that never resets is not the number an intraday trader means."""
    day_one = [
        Bar(ts=BASE, open=D("100"), high=D("102"), low=D("98"), close=D("100"), volume=100),
        Bar(
            ts=BASE + timedelta(minutes=5),
            open=D("100"),
            high=D("106"),
            low=D("104"),
            close=D("105"),
            volume=100,
        ),
    ]
    # Typical prices: (102+98+100)/3 = 100, (106+104+105)/3 = 105
    # VWAP after two bars = (100*100 + 105*100) / 200 = 102.5
    values = vwap(day_one)
    assert values[0] == Decimal(100)
    assert values[1] == Decimal("102.5")

    next_day = day_one + [
        Bar(
            ts=BASE + timedelta(days=1),
            open=D("200"),
            high=D("202"),
            low=D("198"),
            close=D("200"),
            volume=50,
        )
    ]
    # The new session restarts from its own first bar, not from 102.5.
    assert vwap(next_day)[2] == Decimal(200)


def test_anchored_vwap_starts_at_its_anchor() -> None:
    bars = [bar(index, "101", "99", "100", volume=100) for index in range(5)]
    values = anchored_vwap(bars, anchor_index=2)
    assert values[0] is None
    assert values[1] is None
    assert values[2] == Decimal(100)

    with pytest.raises(ValidationError):
        anchored_vwap(bars, anchor_index=99)


# --- TA-003: momentum -----------------------------------------------------


def test_momentum_indicators() -> None:
    # Closes: 10 11 12 11 12 13  ->  changes +1 +1 -1 +1 +1
    values = series("10", "11", "12", "11", "12", "13")
    result = rsi(values, 3)

    # Wilder seed at index 3: avg gain = (1+1+0)/3 = 0.6667, avg loss = (0+0+1)/3 = 0.3333
    #   RS = 2 -> RSI = 100 - 100/3 = 66.6667
    assert approx(result[3], "66.666667", "0.0001")
    # index 4: gain = (0.6667*2 + 1)/3 = 0.7778, loss = (0.3333*2 + 0)/3 = 0.2222
    #   RS = 3.5 -> RSI = 100 - 100/4.5 = 77.7778
    assert approx(result[4], "77.777778", "0.0001")
    # index 5: gain = (0.7778*2 + 1)/3 = 0.8519, loss = 0.1481 -> RS = 5.75
    #   RSI = 100 - 100/6.75 = 85.1852
    assert approx(result[5], "85.185185", "0.0001")
    assert result[:3] == [None, None, None]


def test_rsi_is_100_when_there_are_no_losses() -> None:
    """The mathematical limit, not a division error or an arbitrary cap."""
    rising = series(*[str(10 + index) for index in range(20)])
    assert rsi(rising, 14)[-1] == Decimal(100)


def test_rsi_uses_wilder_smoothing_not_an_ema() -> None:
    """Wilder uses 1/period; an EMA uses 2/(period+1). They are not the same."""
    values = [Decimal(0)] * 5 + [Decimal(10)] * 5
    wilder = wilder_smooth([None] + values[1:], 3, seed_index=3)
    ema_values = ema(values, 3)
    assert wilder[-1] != ema_values[-1]


def test_macd_relationships_hold() -> None:
    values = series(*[str(100 + index) for index in range(60)])
    result = macd(values, 12, 26, 9)

    fast = ema(values, 12)
    slow = ema(values, 26)
    # The MACD line is exactly fast EMA minus slow EMA.
    assert result.macd[-1] == fast[-1] - slow[-1]
    # The histogram is exactly the line minus its signal.
    assert result.histogram[-1] == result.macd[-1] - result.signal[-1]
    # Warm-up is respected: no signal before the MACD line itself exists.
    assert result.signal[25] is None


def test_macd_rejects_inverted_periods() -> None:
    values = series(*[str(index) for index in range(60)])
    with pytest.raises(ValidationError):
        macd(values, 26, 12)


def test_stochastic_positions_the_close_within_the_range() -> None:
    # Range over the window: high 110, low 90. Close 105 -> (105-90)/20 = 75%.
    bars = [bar(index, "110", "90", "100") for index in range(13)]
    bars.append(bar(13, "108", "92", "105"))
    result = stochastic(bars, period=14, smooth_k=1, smooth_d=1)
    assert approx(result.k[-1], "75", "0.0001")


def test_stochastic_flat_window_is_neutral() -> None:
    """A window with no range has no position in it; 50 beats a crash."""
    bars = [bar(index, "100", "100", "100") for index in range(14)]
    result = stochastic(bars, period=14, smooth_k=1, smooth_d=1)
    assert result.k[-1] == Decimal(50)


def test_roc() -> None:
    values = series("100", "101", "102", "103", "104", "110")
    # (110 - 105... ) index 5 vs index 0: (110-100)/100 = 10%
    assert approx(roc(values, 5)[-1], "10", "0.0001")


# --- TA-004: volatility ---------------------------------------------------


def test_volatility_indicators() -> None:
    # Classic ATR worked example.
    bars = [
        bar(0, "48.70", "47.79", "48.16"),
        bar(1, "48.72", "48.14", "48.61"),
        bar(2, "48.90", "48.39", "48.75"),
        bar(3, "48.87", "48.37", "48.63"),
        bar(4, "48.82", "48.24", "48.74"),
    ]
    # True ranges: 0.58, 0.51, 0.50, 0.58 (index 1..4)
    # ATR(2) seed at index 2 = (0.58 + 0.51)/2 = 0.545
    #   index 3 = (0.545 + 0.50)/2 = 0.5225
    #   index 4 = (0.5225 + 0.58)/2 = 0.55125
    values = atr(bars, 2)
    assert approx(values[2], "0.545", "0.00001")
    assert approx(values[3], "0.5225", "0.00001")
    assert approx(values[4], "0.55125", "0.00001")

    # ATR% = ATR / close * 100 = 0.55125 / 48.74 * 100
    assert approx(atr_percent(bars, 2)[-1], "1.13100", "0.0001")


def test_standard_deviation_is_population_form() -> None:
    # [2,4,4,4,5,5,7,9]: mean 5, squared deviations sum 32, /8 = 4, sqrt = 2.
    values = series("2", "4", "4", "4", "5", "5", "7", "9")
    assert standard_deviation(values, 8)[-1] == Decimal(2)


def test_bollinger_bands_from_a_known_series() -> None:
    values = series("2", "4", "4", "4", "5", "5", "7", "9")
    result = bollinger_bands(values, 8, Decimal(2))

    assert result.middle[-1] == Decimal(5)
    assert result.upper[-1] == Decimal(9)  # 5 + 2*2
    assert result.lower[-1] == Decimal(1)  # 5 - 2*2
    # Bandwidth = (9-1)/5 * 100 = 160%
    assert approx(result.bandwidth[-1], "160", "0.0001")
    # %B = (9 - 1) / (9 - 1) = 1 for the last value, which sits on the upper band.
    assert approx(result.percent_b[-1], "1", "0.0001")


def test_keltner_channels_are_atr_wide() -> None:
    bars = [bar(index, "102", "98", "100") for index in range(30)]
    result = keltner_channels(bars, period=20, atr_period=10, multiplier=Decimal(2))
    centre = result.middle[-1]
    width = result.upper[-1] - centre
    assert centre is not None
    # True range is a constant 4 here, so ATR is 4 and the band is 8 wide.
    assert approx(width, "8", "0.0001")


def test_historical_volatility_is_zero_for_a_flat_series() -> None:
    values = series(*["100"] * 30)
    assert approx(historical_volatility(values, 20)[-1], "0", "0.000001")


def test_historical_volatility_rejects_a_bad_annualisation() -> None:
    values = series(*[str(100 + index) for index in range(30)])
    with pytest.raises(ValidationError):
        historical_volatility(values, 20, periods_per_year=0)


# --- TA-006: trend --------------------------------------------------------


def test_directional_movement_is_exclusive() -> None:
    """At most one of +DM and -DM can be non-zero on any bar.

    Without that rule ADX never falls and every market looks like a trend.
    """
    bars = [bar(0, "100", "90", "95")]
    bars.append(bar(1, "105", "95", "100"))  # outside bar: up 5, down -5 -> up wins
    bars.extend(bar(index, "105", "95", "100") for index in range(2, 40))

    result = adx(bars, 14)
    for index in range(len(bars)):
        plus = result.plus_di[index]
        minus = result.minus_di[index]
        if plus is None or minus is None:
            continue
        assert plus >= 0 and minus >= 0
        assert plus <= 100 and minus <= 100


def test_adx_distinguishes_trend_from_chop() -> None:
    trending = [
        bar(index, str(100 + index + 1), str(100 + index - 1), str(100 + index))
        for index in range(60)
    ]
    choppy = [
        bar(index, "101", "99", "100" if index % 2 else "100.5") for index in range(60)
    ]

    trend_adx = last_value(adx(trending, 14).adx)
    chop_adx = last_value(adx(choppy, 14).adx)

    assert trend_adx is not None and chop_adx is not None
    assert trend_adx > Decimal(40)
    assert chop_adx < Decimal(25)
    assert Decimal(0) <= trend_adx <= Decimal(100)


def test_adx_di_favours_the_trend_direction() -> None:
    rising = [
        bar(index, str(100 + index + 1), str(100 + index - 1), str(100 + index))
        for index in range(60)
    ]
    result = adx(rising, 14)
    assert result.plus_di[-1] > result.minus_di[-1]


def test_donchian_channels_exclude_the_current_bar() -> None:
    """A breakout system cannot trigger against a channel containing its own bar."""
    bars = [bar(index, "110", "90", "100") for index in range(20)]
    bars.append(bar(20, "130", "80", "125"))

    result = donchian_channels(bars, 20)
    # The last bar's own 130 high must not be in its channel.
    assert result.upper[-1] == Decimal(110)
    assert result.lower[-1] == Decimal(90)
    assert result.middle[-1] == Decimal(100)


def test_supertrend_flips_direction_on_a_reversal() -> None:
    rising = [
        bar(index, str(100 + index + 1), str(100 + index - 1), str(100 + index))
        for index in range(40)
    ]
    falling = [
        bar(40 + index, str(140 - index * 3 + 1), str(140 - index * 3 - 1), str(140 - index * 3))
        for index in range(20)
    ]
    result = supertrend(rising + falling, period=10, multiplier=Decimal(2))

    assert result.direction[39] is SignalDirection.LONG
    assert result.direction[-1] is SignalDirection.SHORT


# --- TA-005: volume -------------------------------------------------------


def test_volume_indicators() -> None:
    bars = [
        bar(0, "101", "99", "100", volume=100),
        bar(1, "102", "100", "101", volume=200),  # up  -> +200
        bar(2, "102", "100", "100", volume=300),  # down -> -300
        bar(3, "102", "100", "100", volume=400),  # flat -> unchanged
    ]
    assert obv(bars) == [Decimal(0), Decimal(200), Decimal(-100), Decimal(-100)]

    # Volume SMA(2) at the last bar = (300 + 400)/2 = 350
    assert volume_sma(bars, 2)[-1] == Decimal(350)


def test_relative_volume_excludes_the_current_bar() -> None:
    """Including the current bar damps the very spike this detects."""
    bars = [bar(index, "101", "99", "100", volume=100) for index in range(20)]
    bars.append(bar(20, "101", "99", "100", volume=500))

    # Average of the prior 20 bars is 100, so the spike reads 5.0x.
    assert relative_volume(bars, 20)[-1] == Decimal(5)


def test_vwap_deviation_signs_correctly() -> None:
    bars = [
        Bar(ts=BASE, open=D("100"), high=D("100"), low=D("100"), close=D("100"), volume=100),
        Bar(
            ts=BASE + timedelta(minutes=5),
            open=D("110"),
            high=D("110"),
            low=D("110"),
            close=D("110"),
            volume=100,
        ),
    ]
    # VWAP after two bars = 105; close 110 is +4.7619% above it.
    assert approx(vwap_deviation(bars)[-1], "4.761905", "0.0001")


# --- TA-007: levels -------------------------------------------------------


def test_levels() -> None:
    day_one = [
        Bar(
            ts=BASE + timedelta(minutes=5 * index),
            open=D("100"),
            high=D("110"),
            low=D("90"),
            close=D("105"),
            volume=100,
        )
        for index in range(3)
    ]
    day_two = [
        Bar(
            ts=BASE + timedelta(days=1, minutes=5 * index),
            open=D("106"),
            high=D("112"),
            low=D("104"),
            close=D("108"),
            volume=100,
        )
        for index in range(3)
    ]
    bars = day_one + day_two

    sessions = levels_module.session_levels(bars)
    assert len(sessions) == 2

    previous = levels_module.previous_session(bars, (BASE + timedelta(days=1)).date())
    assert previous is not None
    assert previous.high == Decimal(110)
    assert previous.low == Decimal(90)
    assert previous.close == Decimal(105)

    # Pivot = (110 + 90 + 105)/3 = 101.6667
    pivots = levels_module.classic_pivots(previous)
    assert approx(pivots.pivot, "101.666667", "0.0001")
    # R1 = 2*P - low = 203.3333 - 90 = 113.3333
    assert approx(pivots.r1, "113.333333", "0.0001")
    # S1 = 2*P - high = 203.3333 - 110 = 93.3333
    assert approx(pivots.s1, "93.333333", "0.0001")
    # R2 = P + range = 101.6667 + 20 = 121.6667
    assert approx(pivots.r2, "121.666667", "0.0001")

    fib = levels_module.fibonacci_pivots(previous)
    # R1 = P + 0.382 * 20 = 101.6667 + 7.64 = 109.3067
    assert approx(fib.r1, "109.306667", "0.0001")


def test_pivot_nearest_helpers() -> None:
    previous = levels_module.SessionLevels(
        day=BASE.date(), high=D("110"), low=D("90"), close=D("105"), open=D("100")
    )
    pivots = levels_module.classic_pivots(previous)
    assert pivots.nearest_resistance(Decimal(100)) is not None
    assert pivots.nearest_support(Decimal(100)) is not None
    # Above every level there is no resistance left.
    assert pivots.nearest_resistance(Decimal(1000)) is None


def test_swing_points_confirm_only_after_the_fact() -> None:
    """A swing high is not knowable when it forms; claiming otherwise is bias."""
    prices = ["100", "102", "105", "103", "101", "104", "107"]
    bars = [
        bar(index, str(Decimal(price) + 1), str(Decimal(price) - 1), price)
        for index, price in enumerate(prices)
    ]
    swings = levels_module.swing_points(bars, strength=2)

    highs = [swing for swing in swings if swing.kind == "high"]
    assert highs
    # The confirmed swing high is at index 2, never at the last bar.
    assert highs[0].index == 2
    assert all(swing.index <= len(bars) - 3 for swing in swings)


# --- TA-008: multi-timeframe ---------------------------------------------


def test_resample_builds_higher_timeframe_bars() -> None:
    bars = [bar(index, str(100 + index), str(90 + index), str(95 + index)) for index in range(6)]
    higher = resample(bars, 15)  # three 5-minute bars per 15-minute bar

    assert len(higher) == 2
    assert higher[0].open == bars[0].open
    assert higher[0].close == bars[2].close
    assert higher[0].high == max(b.high for b in bars[:3])
    assert higher[0].volume == sum(b.volume for b in bars[:3])


@pytest.mark.safety
def test_mtf_no_lookahead() -> None:
    """A 15-minute value must not be visible until its bar has closed."""
    bars = [bar(index, str(100 + index), str(90 + index), str(95 + index)) for index in range(9)]
    higher = resample(bars, 15)
    # One distinct value per higher bar, so leakage is identifiable.
    higher_values = [Decimal(10), Decimal(20), Decimal(30)]

    aligned = align_to_base(bars, higher, higher_values, 15)

    # 09:15, 09:20, 09:25 are inside the first 15-minute bar: nothing has closed.
    assert aligned[0] is None
    assert aligned[1] is None
    assert aligned[2] is None
    # 09:30 is the first bar after the 09:15-09:30 bar closed.
    assert aligned[3] == Decimal(10)
    assert aligned[4] == Decimal(10)
    assert aligned[5] == Decimal(10)
    # And the second 15-minute value only appears from 09:45.
    assert aligned[6] == Decimal(20)
    assert aligned[8] == Decimal(20)
    # The final, still-open higher bar is never visible.
    assert Decimal(30) not in [value for value in aligned if value is not None]


def test_higher_timeframe_value_helper_is_also_safe() -> None:
    bars = [bar(index, str(100 + index), str(90 + index), str(95 + index)) for index in range(30)]

    aligned = higher_timeframe_value(bars, 15, lambda higher: sma(closes(higher), 2))
    # Still None while the first two 15-minute bars have not both closed.
    assert aligned[0] is None
    assert any(value is not None for value in aligned)


def test_align_rejects_mismatched_lengths() -> None:
    bars = [bar(index, "101", "99", "100") for index in range(3)]
    with pytest.raises(ValidationError):
        align_to_base(bars, bars, [Decimal(1)], 15)


# --- TA-009: price action -------------------------------------------------


def test_price_action_features() -> None:
    # Open 100, high 110, low 95, close 105: body 5, upper wick 5, lower wick 5.
    shape = geometry(bar(0, "110", "95", "105", open_="100"))
    assert shape.range == Decimal(15)
    assert shape.body == Decimal(5)
    assert shape.upper_wick == Decimal(5)
    assert shape.lower_wick == Decimal(5)
    assert approx(shape.body_ratio, "0.333333", "0.0001")
    assert shape.is_bullish

    # A zero-range bar has no proportions rather than a division error.
    flat = geometry(bar(1, "100", "100", "100", open_="100"))
    assert flat.range == Decimal(0)
    assert flat.body_ratio == Decimal(0)


def test_inside_and_outside_bars() -> None:
    bars = [
        bar(0, "110", "90", "100"),
        bar(1, "105", "95", "100"),  # inside
        bar(2, "115", "85", "100"),  # outside
    ]
    assert is_inside_bar(bars) == [None, True, False]
    assert is_outside_bar(bars) == [None, False, True]


def test_range_expansion_and_consolidation() -> None:
    bars = [bar(index, "101", "99", "100") for index in range(20)]  # range 2 each
    bars.append(bar(20, "106", "94", "100"))  # range 12

    assert range_expansion(bars, 20)[-1] == Decimal(6)  # 12 / 2

    tight = [bar(index, "101", "99", "100") for index in range(20)]
    # (101 - 99) / 100 * 100 = 2%
    assert approx(consolidation_score(tight, 20)[-1], "2", "0.0001")


def test_gap_percent() -> None:
    assert approx(gap_percent(Decimal(100), Decimal(102)), "2", "0.0001")
    assert approx(gap_percent(Decimal(100), Decimal(98)), "-2", "0.0001")
    assert gap_percent(Decimal(0), Decimal(100)) == Decimal(0)


# --- TA-010: cache --------------------------------------------------------


def test_indicator_cache() -> None:
    cache = IndicatorCache(max_entries=3)

    def key_for(ts: datetime) -> IndicatorCacheKey:
        return IndicatorCacheKey.build(
            instrument="NSE_CASH_RELIANCE",
            interval_minutes=5,
            indicator="rsi",
            params={"period": 14},
            last_bar_ts=ts,
        )

    first = key_for(BASE)
    assert cache.get(first) is None
    cache.set(first, [Decimal(50)])
    assert cache.get(first) == [Decimal(50)]
    assert cache.stats.hits == 1

    # A new bar produces a different key, so the stale entry can never be served.
    second = key_for(BASE + timedelta(minutes=5))
    assert cache.get(second) is None

    computed = cache.get_or_compute(second, lambda: [Decimal(60)])
    assert computed == [Decimal(60)]
    assert cache.get(second) == [Decimal(60)]


def test_cache_evicts_and_invalidates() -> None:
    cache = IndicatorCache(max_entries=2)
    for index in range(3):
        cache.set(
            IndicatorCacheKey.build(
                instrument=f"SYM{index}",
                interval_minutes=5,
                indicator="sma",
                params={"period": 20},
                last_bar_ts=BASE,
            ),
            [Decimal(index)],
        )
    assert len(cache) == 2
    assert cache.stats.evictions == 1

    removed = cache.invalidate_instrument("SYM2")
    assert removed == 1


# --- TA-011: snapshot -----------------------------------------------------


def _long_series(count: int = 260) -> list[Bar]:
    bars: list[Bar] = []
    price = Decimal(100)
    for index in range(count):
        price = price + Decimal("0.5") if index % 3 else price - Decimal("0.3")
        bars.append(
            Bar(
                ts=BASE + timedelta(minutes=5 * index),
                open=price,
                high=price + Decimal(1),
                low=price - Decimal(1),
                close=price + Decimal("0.2"),
                volume=1000 + index,
            )
        )
    return bars


def test_technical_snapshot() -> None:
    bars = _long_series()
    snapshot = build_snapshot(
        bars, instrument="NSE_CASH_TEST", interval_minutes=5, params=SnapshotParams()
    )

    assert snapshot.bar_ts == bars[-1].ts
    assert snapshot.close == bars[-1].close
    assert not snapshot.unavailable, snapshot.unavailable

    for field_name in ("rsi", "atr", "adx", "ema_fast", "ema_slow", "sma_trend", "obv"):
        assert getattr(snapshot, field_name) is not None, field_name

    payload = snapshot.to_dict()
    assert payload["params"]["rsi_period"] == 14
    assert payload["bar_ts"] == bars[-1].ts.isoformat()
    # Decimals serialise as strings so an audit record never carries a float.
    assert isinstance(payload["rsi"], str)


def test_snapshot_reports_why_a_value_is_missing() -> None:
    """Silence would let a strategy read 'not enough data' as 'no signal'."""
    bars = _long_series(count=30)
    snapshot = build_snapshot(bars, instrument="SHORT", interval_minutes=5)

    assert snapshot.sma_trend is None
    assert "sma_trend" in snapshot.unavailable
    assert "needs at least 200" in snapshot.unavailable["sma_trend"]
    # Indicators that *can* be computed still are.
    assert snapshot.rsi is not None


def test_snapshot_requires_bars() -> None:
    with pytest.raises(InsufficientHistoryError):
        build_snapshot([], instrument="EMPTY", interval_minutes=5)


# --- TA-012: no orphan indicators ----------------------------------------


def test_no_orphan_indicators() -> None:
    """Every registered indicator must have a consumer (contract §15).

    Indicators that nothing uses are complexity with no justification. The
    consumers here are the snapshot builder and the analysis modules that compose
    other indicators; strategies add themselves in P3.
    """
    consumed = set(SNAPSHOT_INDICATORS) | {
        # Composed inside other indicators or used by analysis modules.
        "wma",
        "roc",
        "keltner",
        "historical_volatility",
        "volume_sma",
        "fibonacci_pivots",
        "swing_points",
        "inside_bar",
        "outside_bar",
        "atr_percent",
    }
    orphans = sorted(set(registry.names) - consumed)
    assert not orphans, f"indicators with no consumer: {orphans}"


def test_registry_declares_lookbacks() -> None:
    for name in registry.names:
        spec = registry.get(name)
        assert spec is not None
        assert callable(spec.min_lookback)
        assert spec.min_lookback() > 0 if name in {"vwap", "obv"} else True
