"""Synthetic equity evidence with independent liquidity, return, beta and gap references."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis.equity import DailyBar, EquitySnapshot, PriceObservation
from app.analysis.events import event_blackout
from app.analysis.liquidity import LiquidityPolicy, screen_liquidity
from app.analysis.preopen import gap_analysis, rank_gaps
from app.analysis.relative_strength import rank_strength
from app.analysis.sectors import sector_mapping
from app.analysis.universe import UniversePolicy, resolve_universe
from app.analysis.volatility import volatility_profile
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_regime import calendar


def snapshot(symbol="TEST", closes=(100, 120, 96), **changes):
    bars = tuple(
        DailyBar(
            closed_at=OBSERVED - timedelta(days=3 - index),
            available_at=OBSERVED - timedelta(days=3 - index),
            open=Decimal(close),
            high=Decimal(close) + 1,
            low=Decimal(close) - 1,
            close=Decimal(close),
            volume=100,
            traded_value=Decimal((index + 1) * 1000),
        )
        for index, close in enumerate(closes)
    )
    values = {
        "symbol": symbol,
        "exchange": Exchange.NSE,
        "effective_at": OBSERVED,
        "known_at": OBSERVED,
        "source": "synthetic-fixture",
        "data_origin": DataOrigin.SYNTHETIC,
        "active": True,
        "restricted": False,
        "fno_eligible": True,
        "indices": ("TEST_INDEX",),
        "sector": "TEST_SECTOR",
        "industry": "TEST_INDUSTRY",
        "daily": bars,
        "price": PriceObservation(value=Decimal(100), observed_at=OBSERVED, available_at=OBSERVED),
        "bid": Decimal(99),
        "ask": Decimal(101),
    }
    values.update(changes)
    return EquitySnapshot(**values)


def liquidity_policy():
    return LiquidityPolicy(
        lookback=3,
        minimum_traded_value=Decimal(1000),
        max_participation=Decimal("0.1"),
        max_spread_fraction=Decimal("0.02"),
        max_quote_age_seconds=60,
        max_history_age_days=4,
    )


def universe_policy():
    return UniversePolicy(
        indices=("TEST_INDEX",),
        exchanges=(Exchange.NSE,),
        minimum_price=Decimal(10),
        maximum_price=Decimal(200),
        require_fno=True,
        liquidity=liquidity_policy(),
    )


def test_liquidity_screen():
    result = screen_liquidity(snapshot(), quantity=2, as_of=OBSERVED, policy=liquidity_policy())
    assert result.average_traded_value == 2000
    assert result.spread_fraction == Decimal("0.02")
    assert result.reasons == ()
    result = screen_liquidity(snapshot(), quantity=3, as_of=OBSERVED, policy=liquidity_policy())
    assert result.reasons == ("POSITION_PARTICIPATION",)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"ask": None}, "SPREAD_UNAVAILABLE"),
        ({"ask": Decimal(102)}, "SPREAD"),
        ({"daily": ()}, "TRADED_VALUE_UNAVAILABLE"),
    ],
)
def test_liquidity_missing_data(changes, reason):
    assert (
        reason
        in screen_liquidity(
            snapshot(**changes), quantity=1, as_of=OBSERVED, policy=liquidity_policy()
        ).reasons
    )


def test_universe_resolution():
    members = [
        snapshot("GOOD"),
        snapshot("UNMAPPED", sector=None),
        snapshot("BANNED", restricted=True),
        snapshot("NOFNO", fno_eligible=False),
        snapshot("NOTINDEX", indices=()),
    ]
    result = resolve_universe(
        members[::-1],
        as_of=OBSERVED,
        quantities={member.key: 1 for member in members},
        policy=universe_policy(),
    )
    assert result.included == ("NSE:GOOD",)
    assert result.excluded["NSE:UNMAPPED"] == ("SECTOR_UNMAPPED",)
    assert result.excluded["NSE:BANNED"] == ("RESTRICTED",)
    assert result.excluded["NSE:NOFNO"] == ("FNO_INELIGIBLE",)
    assert result.excluded["NSE:NOTINDEX"] == ("INDEX_MEMBERSHIP",)
    assert resolve_universe(
        [snapshot()], as_of=OBSERVED, quantities={}, policy=universe_policy()
    ).excluded["NSE:TEST"] == ("POSITION_SIZE_UNAVAILABLE",)


def test_relative_strength():
    index = snapshot("INDEX", (100, 105, 110))
    ranking = rank_strength(
        [snapshot("WEAK", (100, 100, 99)), snapshot("STRONG", (100, 110, 121))],
        index,
        lookback=2,
        as_of=OBSERVED,
        max_history_age=timedelta(days=4),
    )
    assert ranking.ranked == (("NSE:STRONG", Decimal("0.1")), ("NSE:WEAK", Decimal("-0.1")))
    assert not ranking.unavailable


def test_relative_strength_refuses_unaligned_or_future_history():
    member = snapshot()
    changed = (*member.daily[:-1],
        member.daily[-1].model_copy(update={"closed_at": OBSERVED - timedelta(hours=23)}),
    )
    index = snapshot(
        "INDEX", daily=tuple(bar.model_copy(update={"available_at": OBSERVED}) for bar in changed)
    )
    result = rank_strength(
        [member], index, lookback=2, as_of=OBSERVED, max_history_age=timedelta(days=4)
    )
    assert result.ranked == ()
    assert result.unavailable[member.key] == "unaligned benchmark sessions"
    future = snapshot(known_at=OBSERVED + timedelta(seconds=1))
    assert rank_strength(
        [future], member, lookback=2, as_of=OBSERVED, max_history_age=timedelta(days=4)
    ).unavailable


def test_sector_mapping():
    result = sector_mapping([snapshot("MAPPED"), snapshot("MISSING", sector=None)], as_of=OBSERVED)
    assert result.mapped == {"NSE:MAPPED": ("TEST_SECTOR", "TEST_INDUSTRY")}
    assert result.unmapped == ("NSE:MISSING",)


def test_volatility_profile():
    result = volatility_profile(
        snapshot(),
        snapshot("INDEX", (100, 110, 99)),
        as_of=OBSERVED,
        period=2,
        periods_per_year=252,
        max_history_age=timedelta(days=4),
    )
    assert result.atr_percent == Decimal(23) / 96 * 100
    expected_volatility = (Decimal("1.5").ln() / 2) * Decimal(252).sqrt() * 100
    assert abs(result.realised_volatility - expected_volatility) < Decimal("0.00000001")
    assert result.beta == 2
    flat = volatility_profile(
        snapshot(),
        snapshot("INDEX", (100, 100, 100)),
        as_of=OBSERVED,
        period=2,
        periods_per_year=252,
        max_history_age=timedelta(days=4),
    )
    assert flat.beta is None


def test_gap_analysis():
    price = PriceObservation(value=Decimal("105.6"), observed_at=OBSERVED, available_at=OBSERVED)
    member = snapshot(preopen=price)
    arguments = {
        "as_of": OBSERVED,
        "max_age": timedelta(minutes=1),
        "previous_session": (OBSERVED - timedelta(days=1)).date(),
    }
    result = gap_analysis(member, **arguments)
    assert result.gap_percent == 10
    assert result.method == "PREOPEN"
    fallback = gap_analysis(snapshot(official_open=price), **arguments)
    assert fallback.method == "OFFICIAL_OPEN"
    assert gap_analysis(snapshot(), **arguments) is None
    assert rank_gaps([fallback, result])[0].gap_percent == 10
    assert gap_analysis(member, **{**arguments, "previous_session": OBSERVED.date()}) is None


def test_event_blackout_flagging():
    result = event_blackout(
        "TEST", as_of=OBSERVED, calendar=calendar(), restricted_strategies=("trend",)
    )
    assert result.status == "BLACKOUT"
    assert result.event_ids == ("event-test",)
    assert result.restricted_strategies == ("trend",)
    assert (
        event_blackout(
            "TEST", as_of=OBSERVED, calendar=None, restricted_strategies=("trend",)
        ).status
        == "UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"bid": Decimal(102)},
        {"ask": Decimal("NaN")},
        {"known_at": OBSERVED - timedelta(seconds=1)},
        {"daily": snapshot().daily[::-1]},
    ],
)
def test_equity_invalid_evidence(changes):
    with pytest.raises(ValidationError):
        snapshot(**changes)


def test_liquidity_rejects_stale_future_and_invalid_sizes():
    for at in (OBSERVED - timedelta(seconds=1), OBSERVED + timedelta(seconds=61)):
        with pytest.raises(ValueError):
            screen_liquidity(snapshot(), quantity=1, as_of=at, policy=liquidity_policy())
    for quantity in (0, -1, True):
        with pytest.raises(ValueError):
            screen_liquidity(
                snapshot(), quantity=quantity, as_of=OBSERVED, policy=liquidity_policy()
            )
