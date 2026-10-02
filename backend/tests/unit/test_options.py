"""Synthetic option selection and terminal payoff tests with exact reference amounts."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.enums import Exchange, InstrumentType, OptionType, Segment, TransactionType
from app.db.models.instrument import Instrument
from app.fno.expiry import select_expiry
from app.fno.liquidity import OptionLiquidityPolicy, liquidity_failures
from app.fno.options import OptionContract, classify, option_contract
from app.fno.payoff import (
    PayoffLeg,
    StockHolding,
    intrinsic_value,
    payoff,
    payoff_summary,
    time_value,
)
from app.fno.strike_selection import select_strike
from app.fno.structures import OptionStructure, StructureKind, build_structure
from tests.unit.test_option_chain import EXPIRY, OBSERVED, synthetic_chain


def contract(strike=100, side=OptionType.CE, **changes):
    fields = {
        "symbol": f"TEST{strike}{side.value}",
        "underlying": "TEST",
        "exchange": Exchange.NSE,
        "expiry": EXPIRY,
        "strike": Decimal(strike),
        "option_type": side,
        "lot_size": 10,
        "tick_size": Decimal("0.05"),
        "weekly": True,
    }
    fields.update(changes)
    return OptionContract(**fields)


@pytest.mark.parametrize(
    "side,spot,label",
    [
        (OptionType.CE, 100, "ATM"),
        (OptionType.CE, 101, "ITM"),
        (OptionType.CE, 99, "OTM"),
        (OptionType.PE, 100, "ATM"),
        (OptionType.PE, 101, "OTM"),
        (OptionType.PE, 99, "ITM"),
    ],
)
def test_option_classification(side, spot, label):
    assert classify(contract(side=side), Decimal(spot)) == label


def test_option_instrument_validation():
    instrument = Instrument(
        trading_symbol="TEST100CE",
        underlying="TEST",
        exchange=Exchange.NSE,
        segment=Segment.FNO,
        instrument_type=InstrumentType.OPTION,
        expiry_date=EXPIRY,
        strike_price=Decimal(100),
        option_type=OptionType.CE,
        lot_size=10,
        tick_size=Decimal("0.05"),
        is_active=True,
        is_restricted=False,
    )
    assert option_contract(instrument).lot_size == 10
    instrument.is_restricted = True
    with pytest.raises(ValueError):
        option_contract(instrument)
    instrument.is_restricted = False
    instrument.is_active = False
    with pytest.raises(ValueError):
        option_contract(instrument)
    with pytest.raises(ValidationError):
        contract(lot_size=0)
    with pytest.raises(ValidationError):
        contract(tick_size=Decimal("NaN"))


def test_expiry_selection_rules():
    today = contract(expiry=OBSERVED.date())
    weekly = contract()
    monthly = contract(expiry=date(2026, 9, 30), weekly=False)
    assert select_expiry([today, weekly, monthly], as_of=OBSERVED, min_days=2) == EXPIRY
    assert select_expiry([today], as_of=OBSERVED, min_days=2) is None
    assert (
        select_expiry([weekly, monthly], as_of=OBSERVED, min_days=0, kind="MONTHLY")
        == monthly.expiry
    )
    assert select_expiry([contract(weekly=None)], as_of=OBSERVED, min_days=0, kind="WEEKLY") is None
    assert select_expiry([today], as_of=OBSERVED.replace(hour=15, minute=30), min_days=0) is None
    with pytest.raises(ValueError):
        select_expiry([weekly], as_of=OBSERVED, min_days=-1)


def liquid_chain():
    chain = synthetic_chain()
    rows = []
    for index, row in enumerate(chain.strikes):
        rows.append(
            replace(
                row,
                call=replace(
                    row.call,
                    ltp=Decimal(9 - index * 3),
                    bid=Decimal(4),
                    ask=Decimal(5),
                    greeks=replace(
                        row.call.greeks, delta=Decimal("0.8") - Decimal(index) * Decimal("0.3")
                    ),
                ),
                put=replace(row.put, bid=Decimal(4), ask=Decimal(5)),
            )
        )
    return replace(chain, strikes=tuple(rows))


def selection(method, side=OptionType.CE, chain=None, **kwargs):
    return select_strike(
        chain or liquid_chain(),
        [contract(strike, side) for strike in (90, 100, 110)],
        side,
        method=method,
        as_of=OBSERVED,
        max_age=timedelta(minutes=1),
        liquidity=OptionLiquidityPolicy(
            min_oi=1, min_volume=1, max_spread=Decimal(1), max_spread_fraction=Decimal("0.3")
        ),
        **kwargs,
    )


def test_strike_selection():
    assert selection("ATM").strike == 100
    assert selection("OTM").strike == 110
    assert selection("OTM", OptionType.PE).strike == 90
    assert selection("DELTA", target=Decimal("0.25")).strike == 110
    assert selection("PREMIUM", target=Decimal("8")).strike == 90
    assert selection("OTM", steps=2) is None
    with pytest.raises(ValueError):
        selection("DELTA", OptionType.PE, target=Decimal("0.25"))


def test_selection_does_not_substitute_illiquid_or_stale_contracts():
    chain = liquid_chain()
    row = chain.strikes[1]
    changed = replace(
        chain,
        strikes=(
            chain.strikes[0],
            replace(row, call=replace(row.call, volume=None)),
            chain.strikes[2],
        ),
    )
    assert selection("ATM", chain=changed) is None
    stale = tuple(
        replace(
            row,
            call=replace(
                row.call,
                greeks=replace(row.call.greeks, computed_at=OBSERVED - timedelta(minutes=2)),
            ),
        )
        for row in chain.strikes
    )
    assert selection("DELTA", chain=replace(chain, strikes=stale), target=Decimal("0.5")) is None
    with pytest.raises(ValueError):
        selection("ATM", chain=replace(chain, observed_at=OBSERVED + timedelta(seconds=1)))


def test_liquidity_named_failures():
    policy = OptionLiquidityPolicy(
        min_oi=11, min_volume=11, max_spread=Decimal("0.5"), max_spread_fraction=Decimal("0.1")
    )
    assert liquidity_failures(liquid_chain().strikes[0].call, policy) == (
        "OPEN_INTEREST",
        "VOLUME",
        "SPREAD",
    )


@pytest.mark.parametrize(
    "side,transaction,settlement,expected",
    [
        (OptionType.CE, TransactionType.BUY, 120, 150),
        (OptionType.CE, TransactionType.SELL, 120, -150),
        (OptionType.PE, TransactionType.BUY, 80, 150),
        (OptionType.PE, TransactionType.SELL, 80, -150),
        (OptionType.CE, TransactionType.BUY, 90, -50),
    ],
)
def test_option_payoff(side, transaction, settlement, expected):
    leg = PayoffLeg(contract=contract(side=side), side=transaction, lots=1, premium=Decimal(5))
    assert payoff((leg,), Decimal(settlement)) == expected


def test_intrinsic_time_value_and_breakeven():
    assert intrinsic_value(Decimal(120), Decimal(100), OptionType.CE) == 20
    assert time_value(Decimal(5), Decimal(120), Decimal(100), OptionType.CE) == -15
    leg = PayoffLeg(contract=contract(), side=TransactionType.BUY, lots=2, premium=Decimal(5))
    summary = payoff_summary((leg,))
    assert summary.max_loss == 100
    assert summary.max_profit is None
    assert summary.breakevens == (Decimal(105),)
    short = PayoffLeg(contract=contract(), side=TransactionType.SELL, lots=1, premium=Decimal(5))
    assert payoff_summary((short,)).max_loss is None
    with pytest.raises(ValueError):
        payoff((leg,), Decimal(-1))
    with pytest.raises(ValidationError):
        PayoffLeg(contract=contract(), side=TransactionType.BUY, lots=1, premium=Decimal("1.03"))


@pytest.mark.parametrize(
    "kind,sides,strikes,premiums,loss,profit",
    [
        ("LONG_CALL", ["CE"], [100], [5], 50, None),
        ("LONG_PUT", ["PE"], [100], [5], 50, 950),
        ("BULL_CALL_SPREAD", ["CE", "CE"], [100, 110], [8, 3], 50, 50),
        ("BEAR_CALL_SPREAD", ["CE", "CE"], [100, 110], [8, 3], 50, 50),
        ("BULL_PUT_SPREAD", ["PE", "PE"], [90, 100], [3, 8], 50, 50),
        ("BEAR_PUT_SPREAD", ["PE", "PE"], [90, 100], [3, 8], 50, 50),
        ("LONG_STRADDLE", ["CE", "PE"], [100, 100], [5, 5], 100, None),
        ("LONG_STRANGLE", ["PE", "CE"], [90, 110], [3, 3], 60, None),
        ("IRON_CONDOR", ["PE", "PE", "CE", "CE"], [80, 90, 110, 120], [1, 3, 3, 1], 60, 40),
    ],
)
def test_option_structures(kind, sides, strikes, premiums, loss, profit):
    contracts = tuple(contract(strike, OptionType(side))
                      for strike, side in zip(strikes, sides, strict=True))
    structure = build_structure(
        StructureKind(kind), contracts, tuple(Decimal(value) for value in premiums), lots=1
    )
    summary = payoff_summary(structure.legs)
    assert summary.max_loss == loss
    assert summary.max_profit == profit
    assert all(leg.quantity == 10 for leg in structure.legs)
    assert OptionStructure.model_validate_json(structure.model_dump_json()) == structure


def test_covered_call_and_protective_put():
    stock = StockHolding(underlying="TEST", shares=10, cost_per_share=Decimal(100))
    covered = build_structure(
        StructureKind.COVERED_CALL, (contract(110),), (Decimal(5),), lots=1, stock=stock
    )
    assert payoff(covered.legs, Decimal(200), stock) == 150
    assert payoff_summary(covered.legs, stock).max_loss == 950
    protected = build_structure(
        StructureKind.PROTECTIVE_PUT,
        (contract(90, OptionType.PE),),
        (Decimal(5),),
        lots=1,
        stock=stock,
    )
    assert payoff(protected.legs, Decimal(0), stock) == -150
    assert payoff_summary(protected.legs, stock).max_loss == 150
    with pytest.raises(ValueError):
        build_structure(StructureKind.COVERED_CALL, (contract(110),), (Decimal(5),), lots=1)


def test_structure_rejects_mismatched_legs_and_expiries():
    for other in (
        contract(110, expiry=date(2026, 10, 1)),
        contract(110, lot_size=20),
        contract(90),
        contract(110, underlying="OTHER"),
    ):
        with pytest.raises(ValueError):
            build_structure(
                StructureKind.BULL_CALL_SPREAD,
                (contract(), other),
                (Decimal(5), Decimal(3)),
                lots=1,
            )


def test_condor_payoff_reference_and_breakevens():
    structure = build_structure(
        StructureKind.IRON_CONDOR,
        (contract(80, OptionType.PE), contract(90, OptionType.PE), contract(110), contract(120)),
        (Decimal(1), Decimal(3), Decimal(3), Decimal(1)),
        lots=1,
    )
    assert [payoff(structure.legs, Decimal(price)) for price in (0, 85, 100, 115, 1000)] == [
        -60,
        -10,
        40,
        -10,
        -60,
    ]
    assert payoff_summary(structure.legs).breakevens == (Decimal(86), Decimal(114))


def test_zero_payoff_interval_is_not_reported_as_single_root():
    legs = (PayoffLeg(contract=contract(), side=TransactionType.BUY, lots=1, premium=Decimal(0)),)
    assert payoff_summary(legs).zero_intervals == ((Decimal(0), Decimal(100)),)
