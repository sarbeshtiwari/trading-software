"""Black-Scholes, Greeks, IV and portfolio aggregation.

Covers GRK-001…GRK-008, TEST-006.

The reference case throughout is the standard textbook one:
S=100, K=100, T=1 year, r=5%, sigma=20%, no dividend. Published values:

    call  10.4506      put   5.5735
    delta 0.6368 (call), -0.3632 (put)
    gamma 0.018762
    vega  0.37524 per volatility point
    theta -6.414/year = -0.017573/day (call)
    rho   0.53232 per rate point (call)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.clock import IST, FakeClock
from app.core.enums import GreekSource, InstrumentType, OptionType
from app.core.errors import StaleDataError, ValidationError
from app.fno.greeks.analytic import compute_greeks, delta, gamma, rho, theta, vega
from app.fno.greeks.black_scholes import (
    BlackScholesInputs,
    d1_d2,
    intrinsic_value,
    norm_cdf,
    price,
    time_value,
)
from app.fno.greeks.iv import implied_volatility
from app.fno.greeks.params import PricingAssumptions
from app.fno.greeks.portfolio import GreekPosition, aggregate_greeks
from app.fno.greeks.resolver import GreeksResolver
from app.fno.greeks.time import (
    EXPIRY_TIME,
    calendar_years_to_expiry,
    days_to_expiry,
    expiry_moment,
    is_expiry_day,
    sessions_to_expiry,
    trading_years_to_expiry,
)
from app.marketdata.models import Greeks
from app.marketdata.staleness import FreshnessPolicy

pytestmark = pytest.mark.unit


def reference(option_type: OptionType = OptionType.CE, **overrides) -> BlackScholesInputs:
    params = dict(
        spot=100.0,
        strike=100.0,
        time_to_expiry=1.0,
        volatility=0.20,
        rate=0.05,
        dividend_yield=0.0,
        option_type=option_type,
    )
    params.update(overrides)
    return BlackScholesInputs(**params)


def close_to(value, expected: str, tolerance: str = "0.0001") -> bool:
    return abs(Decimal(str(value)) - Decimal(expected)) <= Decimal(tolerance)


# --- GRK-001 --------------------------------------------------------------


def test_black_scholes_pricing() -> None:
    assert close_to(price(reference()), "10.4506", "0.0002")
    assert close_to(price(reference(OptionType.PE)), "5.5735", "0.0002")


def test_put_call_parity_holds() -> None:
    """C - P = S - K e^(-rT). A model that breaks parity is arbitrageable."""
    import math

    call = float(price(reference(OptionType.CE)))
    put = float(price(reference(OptionType.PE)))
    expected = 100.0 - 100.0 * math.exp(-0.05 * 1.0)
    assert abs((call - put) - expected) < 0.0005


def test_d1_and_d2_reference_values() -> None:
    # d1 = (ln(1) + (0.05 + 0.02)) / 0.2 = 0.35; d2 = d1 - 0.2 = 0.15
    d1, d2 = d1_d2(reference())
    assert close_to(d1, "0.35", "0.000001")
    assert close_to(d2, "0.15", "0.000001")
    assert close_to(norm_cdf(0.35), "0.636831", "0.000001")


def test_price_at_expiry_is_intrinsic_value() -> None:
    """No time value can exist once there is no time."""
    itm_call = reference(spot=110.0, time_to_expiry=0.0)
    assert price(itm_call) == Decimal("10.000000")

    otm_call = reference(spot=90.0, time_to_expiry=0.0)
    assert price(otm_call) == Decimal("0.000000")

    itm_put = reference(OptionType.PE, spot=90.0, time_to_expiry=0.0)
    assert price(itm_put) == Decimal("10.000000")


def test_zero_volatility_is_also_intrinsic() -> None:
    assert price(reference(spot=110.0, volatility=0.0)) == Decimal("10.000000")


def test_intrinsic_and_time_value_split() -> None:
    assert intrinsic_value(110.0, 100.0, OptionType.CE) == 10.0
    assert intrinsic_value(90.0, 100.0, OptionType.CE) == 0.0
    assert intrinsic_value(90.0, 100.0, OptionType.PE) == 10.0

    inputs = reference(spot=110.0)
    # Model price minus 10 of intrinsic.
    assert time_value(inputs) == price(inputs) - Decimal("10.000000")
    # With a market price supplied, the split uses that instead.
    assert time_value(inputs, market_price=15.0) == Decimal("5.000000")


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BlackScholesInputs(spot=0, strike=100, time_to_expiry=1, volatility=0.2)
    with pytest.raises(ValidationError):
        BlackScholesInputs(spot=100, strike=-1, time_to_expiry=1, volatility=0.2)
    with pytest.raises(ValidationError):
        BlackScholesInputs(spot=100, strike=100, time_to_expiry=-1, volatility=0.2)
    with pytest.raises(ValidationError):
        d1_d2(reference(time_to_expiry=0.0))


# --- GRK-002 --------------------------------------------------------------


def test_greeks_reference_values() -> None:
    call = reference(OptionType.CE)
    assert close_to(delta(call), "0.636831", "0.00001")
    assert close_to(gamma(call), "0.018762", "0.00001")
    assert close_to(vega(call), "0.375240", "0.00001")
    assert close_to(theta(call), "-0.017573", "0.00001")
    assert close_to(rho(call), "0.532325", "0.00001")


def test_put_greeks_follow_parity() -> None:
    call = reference(OptionType.CE)
    put = reference(OptionType.PE)

    # delta_put = delta_call - 1 (no dividend)
    assert close_to(delta(put), str(delta(call) - 1.0), "0.000001")
    # Gamma and vega are identical for calls and puts.
    assert close_to(gamma(put), str(gamma(call)), "0.000001")
    assert close_to(vega(put), str(vega(call)), "0.000001")
    # Rho has the opposite sign.
    assert rho(put) < 0 < rho(call)


def test_greeks_collapse_at_expiry() -> None:
    """An expired option is stock or nothing; there is no sensitivity between."""
    itm = reference(spot=110.0, time_to_expiry=0.0)
    otm = reference(spot=90.0, time_to_expiry=0.0)

    assert delta(itm) == 1.0
    assert delta(otm) == 0.0
    assert delta(reference(OptionType.PE, spot=90.0, time_to_expiry=0.0)) == -1.0
    assert gamma(itm) == 0.0
    assert theta(itm) == 0.0
    assert vega(itm) == 0.0


def test_deep_itm_and_otm_deltas() -> None:
    assert delta(reference(spot=200.0)) > 0.99
    assert delta(reference(spot=10.0)) < 0.01


def test_theta_is_negative_for_long_options() -> None:
    assert theta(reference(OptionType.CE)) < 0
    assert theta(reference(OptionType.PE)) < 0


def test_gamma_peaks_at_the_money() -> None:
    atm = gamma(reference(spot=100.0))
    itm = gamma(reference(spot=130.0))
    otm = gamma(reference(spot=70.0))
    assert atm > itm and atm > otm


def test_compute_greeks_bundles_everything() -> None:
    result = compute_greeks(reference())
    assert result.source is GreekSource.COMPUTED
    assert close_to(result.delta, "0.636831", "0.00001")
    assert close_to(result.price, "10.4506", "0.0002")
    assert set(result.to_dict()) == {
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "price",
        "source",
    }


def test_dividend_yield_reduces_call_delta() -> None:
    without = delta(reference(dividend_yield=0.0))
    with_dividend = delta(reference(dividend_yield=0.03))
    assert with_dividend < without


# --- GRK-003 --------------------------------------------------------------


def test_iv_solver_roundtrip() -> None:
    """price -> IV -> price must return the volatility that produced it."""
    for volatility in (0.10, 0.15, 0.20, 0.35, 0.60):
        for option_type in (OptionType.CE, OptionType.PE):
            for spot in (90.0, 100.0, 110.0):
                market = float(
                    price(reference(option_type, spot=spot, volatility=volatility))
                )
                result = implied_volatility(
                    market_price=market,
                    spot=spot,
                    strike=100.0,
                    time_to_expiry=1.0,
                    option_type=option_type,
                    rate=0.05,
                )
                assert result.ok, f"{option_type} {spot} {volatility}: {result.reason}"
                assert close_to(result.value, str(volatility * 100), "0.01")


@pytest.mark.safety
def test_iv_non_convergence_returns_none_with_a_reason() -> None:
    """A fabricated IV would poison vega, skew and every options decision."""
    # Below the no-arbitrage floor: the quote is wrong, not the volatility.
    # (The floor is the DISCOUNTED bound, so a deep ITM European put trading
    # under undiscounted intrinsic is still perfectly valid — see the round-trip
    # test, which covers exactly that case.)
    below = implied_volatility(
        market_price=1.0,
        spot=120.0,
        strike=100.0,
        time_to_expiry=1.0,
        option_type=OptionType.CE,
    )
    assert below.value is None
    assert not below.converged
    assert "no-arbitrage floor" in below.reason

    # Above the model maximum.
    above = implied_volatility(
        market_price=99.0,
        spot=100.0,
        strike=100.0,
        time_to_expiry=1.0,
        option_type=OptionType.CE,
    )
    assert above.value is None
    assert "exceeds the model maximum" in above.reason

    # Expired.
    expired = implied_volatility(
        market_price=5.0,
        spot=100.0,
        strike=100.0,
        time_to_expiry=0.0,
        option_type=OptionType.CE,
    )
    assert expired.value is None
    assert "expired" in expired.reason

    # Nonsensical price.
    negative = implied_volatility(
        market_price=-1.0,
        spot=100.0,
        strike=100.0,
        time_to_expiry=1.0,
        option_type=OptionType.CE,
    )
    assert negative.value is None


def test_iv_converges_quickly_for_a_typical_option() -> None:
    market = float(price(reference(volatility=0.22)))
    result = implied_volatility(
        market_price=market,
        spot=100.0,
        strike=100.0,
        time_to_expiry=1.0,
        option_type=OptionType.CE,
        rate=0.05,
    )
    assert result.ok
    assert result.iterations < 20


# --- GRK-005 --------------------------------------------------------------


def test_greeks_assumption_recording(fake_clock: FakeClock) -> None:
    """A delta computed at 6.5% is not the same number as one at 7%."""
    resolver = GreeksResolver(
        assumptions=PricingAssumptions(rate=0.065, version="1.0.0"), clock=fake_clock
    )
    expiry = fake_clock.today() + timedelta(days=30)

    greeks = resolver.compute(
        spot=24500.0,
        strike=24500.0,
        expiry=expiry,
        option_type=OptionType.CE,
        volatility=0.13,
    )
    assert greeks.source is GreekSource.COMPUTED
    assert greeks.assumptions["rate"] == 0.065
    assert greeks.assumptions["version"] == "1.0.0"
    assert "applied_at" in greeks.assumptions

    higher = GreeksResolver(
        assumptions=PricingAssumptions(rate=0.09), clock=fake_clock
    ).compute(
        spot=24500.0,
        strike=24500.0,
        expiry=expiry,
        option_type=OptionType.CE,
        volatility=0.13,
    )
    assert higher.delta != greeks.delta


# --- GRK-004 --------------------------------------------------------------


def test_greeks_source_labelling(fake_clock: FakeClock) -> None:
    resolver = GreeksResolver(clock=fake_clock)
    expiry = fake_clock.today() + timedelta(days=30)

    broker = Greeks(
        delta=Decimal("0.55"),
        gamma=Decimal("0.0004"),
        implied_volatility=Decimal("13.0"),
        source=GreekSource.BROKER,
        computed_at=fake_clock.now(),
    )
    resolved = resolver.resolve(
        broker_greeks=broker,
        spot=24500.0,
        strike=24500.0,
        expiry=expiry,
        option_type=OptionType.CE,
    )
    # Broker Greeks win when present.
    assert resolved.source is GreekSource.BROKER
    assert resolved.greeks.delta == Decimal("0.55")

    # With no broker Greeks, the model fills in and is labelled as such.
    computed = resolver.resolve(
        broker_greeks=None,
        spot=24500.0,
        strike=24500.0,
        expiry=expiry,
        option_type=OptionType.CE,
        fallback_volatility=0.13,
    )
    assert computed.source is GreekSource.COMPUTED
    assert computed.greeks.delta is not None


def test_material_divergence_is_flagged(fake_clock: FakeClock) -> None:
    """If broker and model disagree materially, one of them is wrong."""
    resolver = GreeksResolver(clock=fake_clock)
    expiry = fake_clock.today() + timedelta(days=30)

    absurd = Greeks(
        delta=Decimal("0.05"),  # an ATM call cannot have a 0.05 delta
        implied_volatility=Decimal("13.0"),
        source=GreekSource.BROKER,
        computed_at=fake_clock.now(),
    )
    resolved = resolver.resolve(
        broker_greeks=absurd,
        spot=24500.0,
        strike=24500.0,
        expiry=expiry,
        option_type=OptionType.CE,
    )
    assert resolved.has_divergence
    assert resolved.divergences[0].field == "delta"


def test_no_source_yields_empty_greeks_not_zeros(fake_clock: FakeClock) -> None:
    resolver = GreeksResolver(clock=fake_clock)
    resolved = resolver.resolve(
        broker_greeks=None,
        spot=24500.0,
        strike=24500.0,
        expiry=fake_clock.today() + timedelta(days=30),
        option_type=OptionType.CE,
        fallback_volatility=None,
    )
    # None, not zero: an unknown delta is not a delta of zero.
    assert resolved.greeks.delta is None


# --- GRK-008 --------------------------------------------------------------


@pytest.mark.safety
def test_stale_greeks_block_entry(fake_clock: FakeClock) -> None:
    resolver = GreeksResolver(
        freshness=FreshnessPolicy(greeks_seconds=60), clock=fake_clock
    )
    greeks = Greeks(delta=Decimal("0.5"), computed_at=fake_clock.now())

    resolver.require_fresh(greeks)  # fresh: no raise

    fake_clock.advance_seconds(61)
    with pytest.raises(StaleDataError):
        resolver.require_fresh(greeks)


# --- GRK-007 --------------------------------------------------------------


def test_time_to_expiry_convention() -> None:
    """Indian options expire at 15:30 IST, not at end of day."""
    expiry = date(2026, 1, 29)
    assert expiry_moment(expiry).time() == EXPIRY_TIME

    # Exactly one day before expiry, at the expiry time.
    one_day_before = datetime(2026, 1, 28, 15, 30, tzinfo=IST)
    years = calendar_years_to_expiry(expiry, now=one_day_before)
    assert close_to(years, str(1 / 365), "0.000001")

    # On expiry morning there are 6.5 hours left, not a whole day.
    expiry_morning = datetime(2026, 1, 29, 9, 0, tzinfo=IST)
    hours_left = calendar_years_to_expiry(expiry, now=expiry_morning) * 365 * 24
    assert close_to(hours_left, "6.5", "0.01")

    # After the close on expiry day there is nothing left.
    assert calendar_years_to_expiry(expiry, now=datetime(2026, 1, 29, 16, 0, tzinfo=IST)) == 0.0
    assert calendar_years_to_expiry(expiry, now=datetime(2026, 2, 1, 10, 0, tzinfo=IST)) == 0.0


def test_trading_and_calendar_conventions_differ_over_a_weekend() -> None:
    """Volatility does not accrue when the market is closed; interest does."""
    from app.core.calendar import TradingCalendar

    calendar = TradingCalendar(complete_years=[2026])
    friday = datetime(2026, 1, 9, 15, 30, tzinfo=IST)
    monday_expiry = date(2026, 1, 12)

    calendar_years = calendar_years_to_expiry(monday_expiry, now=friday)
    trading_years = trading_years_to_expiry(monday_expiry, now=friday, calendar=calendar)

    # Three calendar days, but only one trading session.
    assert close_to(calendar_years * 365, "3", "0.01")
    assert close_to(trading_years * 250, "1", "0.01")


def test_day_and_session_counters() -> None:
    from app.core.calendar import TradingCalendar

    calendar = TradingCalendar(complete_years=[2026])
    now = datetime(2026, 1, 5, 10, 0, tzinfo=IST)  # Monday

    assert days_to_expiry(date(2026, 1, 8), now=now) == 3
    assert is_expiry_day(date(2026, 1, 5), now=now)
    assert not is_expiry_day(date(2026, 1, 6), now=now)
    # Mon, Tue, Wed, Thu inclusive.
    assert sessions_to_expiry(date(2026, 1, 8), now=now, calendar=calendar) == 4


# --- GRK-006 --------------------------------------------------------------


def _option(symbol: str, quantity: int, delta_: str, **greek_values) -> GreekPosition:
    return GreekPosition(
        trading_symbol=symbol,
        instrument_type=InstrumentType.OPTION,
        net_quantity=quantity,
        lot_size=75,
        spot=Decimal("24500"),
        greeks=Greeks(
            delta=Decimal(delta_),
            gamma=Decimal(greek_values.get("gamma", "0.0004")),
            theta=Decimal(greek_values.get("theta", "-12.8")),
            vega=Decimal(greek_values.get("vega", "8.4")),
            rho=Decimal(greek_values.get("rho", "5.0")),
        ),
    )


def test_portfolio_greeks_aggregation() -> None:
    positions = [
        # Long 1 lot (75 units) of a 0.62-delta call.
        _option("NIFTY24500CE", 75, "0.62"),
        # Short 1 lot of a -0.38-delta put: short a put is long delta.
        _option("NIFTY24500PE", -75, "-0.38"),
    ]
    result = aggregate_greeks(positions)

    # Delta: 0.62*75 + (-0.38 * -75) = 46.5 + 28.5 = 75
    assert result.delta == Decimal("75.00")
    # Gamma: 0.0004*75 + 0.0004*(-75) = 0
    assert result.gamma == Decimal("0")
    # Theta: -12.8*75 + (-12.8 * -75) = 0 — the long and short cancel.
    assert result.theta == Decimal("0")
    assert result.positions_included == 2
    assert not result.positions_missing_greeks


def test_short_options_flip_greek_signs() -> None:
    """A short straddle is short gamma and LONG theta."""
    short_straddle = [
        _option("NIFTY24500CE", -75, "0.5", gamma="0.0004", theta="-12.8"),
        _option("NIFTY24500PE", -75, "-0.5", gamma="0.0004", theta="-12.8"),
    ]
    result = aggregate_greeks(short_straddle)

    assert result.delta == Decimal("0.00")
    assert result.gamma < 0
    assert not result.is_long_gamma
    assert result.theta > 0
    assert result.is_long_theta  # decay works in the seller's favour


def test_lot_scaling_is_applied() -> None:
    """Unscaled deltas understate index exposure by the lot size."""
    one_lot = aggregate_greeks([_option("NIFTY24500CE", 75, "0.62")])
    assert one_lot.delta == Decimal("46.50")
    # Delta notional = 0.62 * 75 * 24500
    assert one_lot.delta_notional == Decimal("1139250.00")


def test_futures_contribute_delta_one() -> None:
    position = GreekPosition(
        trading_symbol="NIFTY26SEPFUT",
        instrument_type=InstrumentType.FUTURE,
        net_quantity=-75,
        lot_size=75,
        spot=Decimal("24500"),
    )
    result = aggregate_greeks([position])
    assert result.delta == Decimal("-75")
    assert result.gamma == Decimal("0")
    assert result.delta_notional == Decimal("-1837500.00")


def test_missing_greeks_are_listed_not_skipped() -> None:
    """A delta computed from half the book looks like a complete answer."""
    positions = [
        _option("NIFTY24500CE", 75, "0.62"),
        GreekPosition(
            trading_symbol="NIFTY24600CE",
            instrument_type=InstrumentType.OPTION,
            net_quantity=75,
            lot_size=75,
            greeks=None,
        ),
    ]
    result = aggregate_greeks(positions)

    assert result.positions_included == 1
    assert result.positions_missing_greeks == ["NIFTY24600CE"]
