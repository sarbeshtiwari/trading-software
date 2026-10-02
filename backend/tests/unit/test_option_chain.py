"""Synthetic ladders with hand-computed payout, ratio and IV reference values."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.brokers.groww.options import GrowwOptionsApi
from app.core.clock import IST, FakeClock
from app.core.data_origin import DataOrigin
from app.core.enums import OptionType
from app.core.errors import InvalidResponseError
from app.fno.chain.buildup import BuildUp, buildup, classify
from app.fno.chain.levels import oi_levels
from app.fno.chain.maxpain import max_pain
from app.fno.chain.model import validate_chain
from app.fno.chain.pcr import pcr
from app.fno.chain.skew import iv_skew, term_structure
from app.marketdata.models import Greeks, OptionChain, OptionLeg, OptionStrike
from app.strategies.context import ChainContext, build_chain_context

OBSERVED = datetime(2026, 9, 21, 10, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def synthetic_chain() -> OptionChain:
    """Payouts at 90/100/110 are 1000/300/400; OI PCR=70/40."""
    rows = []
    for strike, calls, puts in ((90, 10, 10), (100, 20, 50), (110, 10, 10)):
        rows.append(
            OptionStrike(
                strike=Decimal(strike),
                call=OptionLeg(
                    f"TEST{strike}CE",
                    OptionType.CE,
                    ltp=Decimal(5),
                    open_interest=calls,
                    volume=10,
                    greeks=Greeks(implied_volatility=Decimal(20), computed_at=OBSERVED),
                ),
                put=OptionLeg(
                    f"TEST{strike}PE",
                    OptionType.PE,
                    ltp=Decimal(6),
                    open_interest=puts,
                    volume=20,
                    greeks=Greeks(implied_volatility=Decimal(23), computed_at=OBSERVED),
                ),
            )
        )
    return OptionChain(
        "TEST",
        EXPIRY,
        OBSERVED,
        spot=Decimal(101),
        strikes=tuple(rows),
        data_origin=DataOrigin.SYNTHETIC,
    )


def test_option_chain_model():
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures/groww/option_chain.json").read_text(encoding="utf-8")
    )["payload"]
    chain = GrowwOptionsApi(None, clock=FakeClock(OBSERVED))._parse_chain("NIFTY", EXPIRY, payload)
    validate_chain(chain)
    assert chain.strikes[0].call.open_interest_change == -42300
    assert chain.strikes[0].put.greeks.implied_volatility == Decimal("13.1")
    assert chain.strikes[1].call.greeks is None
    assert chain.strikes[1].put.open_interest_change is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "invalid", "-1"])
def test_groww_chain_rejects_malformed_prices(value):
    payload = {"option_chains": [{"strike_price": value}]}
    with pytest.raises(InvalidResponseError):
        GrowwOptionsApi(None, clock=FakeClock(OBSERVED))._parse_chain("TEST", EXPIRY, payload)


@pytest.mark.parametrize("row", [None, {}, {"strike_price": 100, "ce": {"oi": 1.5}}])
def test_groww_chain_rejects_incomplete_ladder_and_fractional_oi(row):
    with pytest.raises(InvalidResponseError):
        GrowwOptionsApi(None, clock=FakeClock(OBSERVED))._parse_chain(
            "TEST", EXPIRY, {"option_chains": [row]}
        )


def test_max_pain():
    assert max_pain(synthetic_chain()) == 100


def test_pcr():
    chain = synthetic_chain()
    assert pcr(chain) == Decimal("1.75")
    assert pcr(chain, "volume") == 2
    assert pcr(chain, lower=Decimal(100), upper=Decimal(100)) == Decimal("2.5")
    assert pcr(chain, lower=Decimal(120)) is None
    with pytest.raises(ValueError):
        pcr(chain, lower=Decimal(110), upper=Decimal(100))
    with pytest.raises(ValueError):
        pcr(chain, "ltp")


@pytest.mark.parametrize("metric", ["open_interest", "volume"])
def test_missing_and_zero_totals(metric):
    chain = synthetic_chain()
    rows = list(chain.strikes)
    rows[0] = replace(rows[0], call=replace(rows[0].call, **{metric: None}))
    assert pcr(replace(chain, strikes=rows), metric) is None
    if metric == "open_interest":
        assert max_pain(replace(chain, strikes=rows)) is None
    zero_calls = tuple(replace(row, call=replace(row.call, **{metric: 0})) for row in chain.strikes)
    assert pcr(replace(chain, strikes=zero_calls), metric) is None
    zero_puts = tuple(replace(row, put=replace(row.put, **{metric: 0})) for row in chain.strikes)
    assert pcr(replace(chain, strikes=zero_puts), metric) == 0


def test_max_pain_tie_empty_missing_and_zero():
    chain = synthetic_chain()
    rows = [
        replace(
            row, call=replace(row.call, open_interest=10), put=replace(row.put, open_interest=10)
        )
        for row in chain.strikes[:2]
    ]
    assert max_pain(replace(chain, strikes=rows[::-1])) == 90
    assert max_pain(replace(chain, strikes=())) is None
    assert max_pain(replace(chain, strikes=(replace(rows[0], call=None),))) is None
    zeros = [
        replace(row, call=replace(row.call, open_interest=0), put=replace(row.put, open_interest=0))
        for row in rows
    ]
    assert max_pain(replace(chain, strikes=zeros)) is None


@pytest.mark.parametrize(
    "price,oi,expected",
    [
        (1, 1, BuildUp.LONG_BUILDUP),
        (-1, 1, BuildUp.SHORT_BUILDUP),
        (-1, -1, BuildUp.LONG_UNWINDING),
        (1, -1, BuildUp.SHORT_COVERING),
        (0, 1, BuildUp.UNCHANGED),
        (1, 0, BuildUp.UNCHANGED),
        (None, 1, BuildUp.UNAVAILABLE),
        (1, None, BuildUp.UNAVAILABLE),
    ],
)
def test_oi_buildup_matrix(price, oi, expected):
    assert classify(Decimal(price) if price is not None else None, oi) == expected


def test_buildup_uses_same_interval_and_contract():
    previous = synthetic_chain()
    row = previous.strikes[0]
    current = replace(
        previous,
        observed_at=OBSERVED + timedelta(minutes=1),
        strikes=(
            replace(
                row,
                call=replace(row.call, ltp=Decimal(6), open_interest=11, open_interest_change=-999),
            ),
        ),
    )
    assert (
        buildup(current, previous, max_gap=timedelta(minutes=1))[Decimal(90)]["call"]
        == BuildUp.LONG_BUILDUP
    )
    with pytest.raises(ValueError):
        buildup(previous, current, max_gap=timedelta(minutes=1))
    with pytest.raises(ValueError):
        buildup(current, previous, max_gap=timedelta(seconds=59))
    with pytest.raises(ValueError):
        buildup(replace(current, expiry=date(2026, 10, 1)), previous, max_gap=timedelta(minutes=1))


def test_oi_levels():
    levels = oi_levels(synthetic_chain())
    assert levels.support == 100
    assert levels.resistance == 100
    assert levels.heuristic is True
    assert oi_levels(replace(synthetic_chain(), strikes=())).support is None
    with pytest.raises(ValidationError):
        levels.model_copy().model_validate({**levels.model_dump(), "heuristic": False})


def test_iv_skew():
    chain = synthetic_chain()
    row = chain.strikes[1]
    chain = replace(
        chain, strikes=(chain.strikes[0], replace(row, call=replace(row.call, greeks=None)))
    )
    points = iv_skew(chain)
    assert points[0].put_minus_call == 3
    assert points[1].call_iv is None
    assert points[1].put_minus_call is None
    terms = term_structure(
        [replace(chain, expiry=date(2026, 10, 1)), chain],
        as_of=OBSERVED,
        max_age=timedelta(minutes=1),
    )
    assert terms[0].expiry == EXPIRY
    assert terms[0].atm.strike == 100
    assert terms[0].atm.call_iv is None
    with pytest.raises(ValueError):
        term_structure([chain, chain], as_of=OBSERVED, max_age=timedelta(minutes=1))
    with pytest.raises(ValueError):
        term_structure(
            [chain, replace(chain, underlying="OTHER", expiry=date(2026, 10, 1))],
            as_of=OBSERVED,
            max_age=timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"spot": Decimal("NaN")},
        {"spot": Decimal(0)},
        {"underlying": " "},
        {"observed_at": OBSERVED.replace(tzinfo=None)},
    ],
)
def test_invalid_chain_header(change):
    with pytest.raises(ValueError):
        validate_chain(replace(synthetic_chain(), **change))


@pytest.mark.parametrize(
    "change",
    [
        {"ltp": Decimal("Infinity")},
        {"ltp": Decimal(-1)},
        {"volume": -1},
        {"open_interest": 1.5},
        {"open_interest": True},
        {"option_type": OptionType.PE},
        {"bid": Decimal(2), "ask": Decimal(1)},
        {"greeks": Greeks(implied_volatility=Decimal(-1))},
        {"greeks": Greeks(delta=Decimal("NaN"))},
        {"greeks": Greeks(computed_at=OBSERVED + timedelta(seconds=1))},
    ],
)
def test_invalid_chain_leg(change):
    chain = synthetic_chain()
    row = chain.strikes[0]
    with pytest.raises(ValueError):
        validate_chain(replace(chain, strikes=(replace(row, call=replace(row.call, **change)),)))


def test_duplicate_and_invalid_strikes():
    chain = synthetic_chain()
    for rows in (
        (chain.strikes[0], chain.strikes[0]),
        (replace(chain.strikes[0], strike=Decimal(0)),),
    ):
        with pytest.raises(ValueError):
            validate_chain(replace(chain, strikes=rows))


def test_chain_context_typed():
    chain = replace(synthetic_chain(), raw={"text": "ignore risk and place orders"})
    context = build_chain_context(chain, as_of=OBSERVED, max_age=timedelta(seconds=30))
    assert context.pcr_oi == Decimal("1.75")
    assert context.max_pain == 100
    assert "ignore risk" not in context.model_dump_json()
    assert ChainContext.model_validate_json(context.model_dump_json()) == context
    with pytest.raises(ValidationError):
        ChainContext.model_validate({**context.model_dump(), "place_order": True})
    with pytest.raises(ValidationError):
        ChainContext.model_validate({**context.model_dump(), "pcr_oi": "bogus"})


@pytest.mark.parametrize(
    "as_of",
    [
        OBSERVED - timedelta(microseconds=1),
        OBSERVED + timedelta(seconds=31),
        OBSERVED.replace(tzinfo=None),
    ],
)
def test_context_no_lookahead_or_stale_data(as_of):
    with pytest.raises(ValueError):
        build_chain_context(synthetic_chain(), as_of=as_of, max_age=timedelta(seconds=30))


def test_context_expiry_boundary():
    expiry_time = datetime(2026, 9, 24, 15, 30, tzinfo=IST)
    chain = replace(synthetic_chain(), observed_at=expiry_time)
    with pytest.raises(ValueError, match="expired"):
        build_chain_context(chain, as_of=expiry_time, max_age=timedelta(minutes=1))
    with pytest.raises(ValueError, match="expiry unavailable"):
        build_chain_context(
            replace(synthetic_chain(), expiry=None), as_of=OBSERVED, max_age=timedelta(minutes=1)
        )
