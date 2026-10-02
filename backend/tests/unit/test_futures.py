"""Synthetic futures metadata and hand-calculated carry/rollover reference values."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.enums import Exchange, InstrumentType, Segment, TransactionType
from app.db.models.instrument import Instrument
from app.fno.basis import calendar_spread, futures_basis
from app.fno.futures import FutureContract, resolve_future
from app.fno.rollover import FutureQuote, RolloverPolicy, rollover_plan
from tests.unit.test_option_chain import OBSERVED


def future(expiry=date(2026, 9, 24), lot_size=10):
    return FutureContract(
        symbol=f"TEST{expiry}FUT",
        underlying="TEST",
        exchange=Exchange.NSE,
        expiry=expiry,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
    )


def master(expiry):
    contract = future(expiry)
    return Instrument(
        trading_symbol=contract.symbol,
        underlying=contract.underlying,
        exchange=contract.exchange,
        expiry_date=expiry,
        lot_size=contract.lot_size,
        tick_size=contract.tick_size,
        is_active=True,
        is_restricted=False,
        instrument_type=InstrumentType.FUTURE,
        segment=Segment.FNO,
    )


def quotes():
    return (
        FutureQuote(
            contract=future(), observed_at=OBSERVED, bid=Decimal(100), ask=Decimal(102), volume=100
        ),
        FutureQuote(
            contract=future(date(2026, 10, 29)),
            observed_at=OBSERVED,
            bid=Decimal(104),
            ask=Decimal(106),
            volume=100,
        ),
    )


def policy():
    return RolloverPolicy(
        window_days=3,
        min_volume=50,
        max_spread=Decimal(2),
        max_age_seconds=60,
        max_quote_skew_seconds=5,
    )


def test_futures_contract_resolution():
    expiries = [date(2026, 9, 24), date(2026, 10, 29), date(2026, 11, 26)]
    instruments = [master(date(2026, 8, 27))] + [master(expiry) for expiry in reversed(expiries)]
    for tenor, expiry in zip(("NEAR", "NEXT", "FAR"), expiries, strict=True):
        result = resolve_future(
            instruments, "TEST", exchange=Exchange.NSE, as_of=OBSERVED, tenor=tenor
        )
        assert result.expiry == expiry
        assert result.lot_size == 10
    assert resolve_future(instruments, "MISSING", exchange=Exchange.NSE, as_of=OBSERVED) is None
    result = resolve_future(instruments, "TEST", exchange=Exchange.NSE, as_of=future().expires_at)
    assert result.expiry == expiries[1]


def test_future_resolution_restricted_and_ambiguous():
    near, far = master(date(2026, 9, 24)), master(date(2026, 10, 29))
    near.is_restricted = True
    assert resolve_future([near, far], "TEST", exchange=Exchange.NSE, as_of=OBSERVED) is None
    near.is_restricted = False
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_future([near, near], "TEST", exchange=Exchange.NSE, as_of=OBSERVED)


def test_futures_basis():
    result = futures_basis(
        Decimal(100), Decimal(102), as_of=OBSERVED, expiry=OBSERVED + timedelta(days=73)
    )
    assert result.absolute == 2
    assert result.fraction == Decimal("0.02")
    assert result.annualised_carry == Decimal("0.10")
    negative = futures_basis(
        Decimal(100), Decimal(98), as_of=OBSERVED, expiry=OBSERVED + timedelta(days=73)
    )
    assert negative.annualised_carry == Decimal("-0.10")
    assert calendar_spread(Decimal(102), Decimal(104)) == 2


@pytest.mark.parametrize(
    "spot,expiry",
    [
        (Decimal(0), OBSERVED + timedelta(days=1)),
        (Decimal("NaN"), OBSERVED + timedelta(days=1)),
        (Decimal(100), OBSERVED),
    ],
)
def test_invalid_basis(spot, expiry):
    with pytest.raises(ValueError):
        futures_basis(spot, Decimal(102), as_of=OBSERVED, expiry=expiry)


@pytest.mark.parametrize(
    "quantity,exit_side,entry_side,difference",
    [
        (20, TransactionType.SELL, TransactionType.BUY, 120),
        (-20, TransactionType.BUY, TransactionType.SELL, -40),
    ],
)
def test_futures_rollover_plan(quantity, exit_side, entry_side, difference):
    plan = rollover_plan(
        *quotes(),
        signed_quantity=quantity,
        as_of=OBSERVED,
        policy=policy(),
        estimated_fees=Decimal(10),
    )
    assert plan.exit.side == exit_side
    assert plan.entry.side == entry_side
    assert plan.exit.quantity == plan.entry.quantity == 20
    assert plan.spread_cost == 40
    assert plan.total_estimated_cost == 50
    assert plan.notional_price_difference == difference
    assert plan.estimate == "ESTIMATED"


def test_rollover_window_and_missing_costs():
    assert (
        rollover_plan(
            *quotes(),
            signed_quantity=10,
            as_of=OBSERVED,
            policy=policy().model_copy(update={"window_days": 2}),
        )
        is None
    )
    plan = rollover_plan(*quotes(), signed_quantity=10, as_of=OBSERVED, policy=policy())
    assert plan.fees is None
    assert plan.total_estimated_cost is None


@pytest.mark.parametrize("quantity", [0, 1, 15, True])
def test_rollover_invalid_quantity(quantity):
    with pytest.raises(ValueError):
        rollover_plan(*quotes(), signed_quantity=quantity, as_of=OBSERVED, policy=policy())


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": OBSERVED + timedelta(seconds=1)},
        {"observed_at": OBSERVED - timedelta(seconds=61)},
        {"observed_at": OBSERVED - timedelta(seconds=6)},
        {"volume": 49},
        {"ask": Decimal(107)},
        {"contract": future(lot_size=30)},
    ],
)
def test_rollover_rejects_invalid_quotes_and_lots(change):
    near, far = quotes()
    changed = FutureQuote.model_validate({**far.model_dump(), **change})
    with pytest.raises(ValueError):
        rollover_plan(near, changed, signed_quantity=20, as_of=OBSERVED, policy=policy())


def test_rollover_expiry_and_tick_validation():
    near, far = quotes()
    expiry = near.contract.expires_at
    with pytest.raises(ValueError, match="expired"):
        rollover_plan(
            near.model_copy(update={"observed_at": expiry}),
            far.model_copy(update={"observed_at": expiry}),
            signed_quantity=10,
            as_of=expiry,
            policy=policy(),
        )
    with pytest.raises(ValidationError, match="off-tick"):
        FutureQuote(
            contract=future(),
            observed_at=OBSERVED,
            bid=Decimal("100.01"),
            ask=Decimal(102),
            volume=100,
        )
