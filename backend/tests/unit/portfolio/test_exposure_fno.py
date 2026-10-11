"""FNOR-001/TEST-010: hand-computed notional exposure across CASH and F&O.

Every figure below is computed by hand in the assertion itself, so a change in
the measure has to change an arithmetic literal rather than quietly pass.
"""

from decimal import Decimal

import pytest

from app.core.enums import InstrumentType
from app.risk.exposure_fno import ExposureUnavailable, lot_notional, notional_per_unit

#: A fixture index contract. Lot size always comes from supplied master data;
#: nothing here claims to be the lot size NSE publishes today.
LOT_SIZE = 75
UNDERLYING = Decimal("24000.00")
PREMIUM = Decimal("101.00")


def test_fno_notional_exposure():
    """A single lot is measured on the underlying, not on the premium paid."""
    assert lot_notional(LOT_SIZE, underlying_price=UNDERLYING) == Decimal("1800000.00")
    # The same lot measured on its premium would read as 7575.00 — 237.6 times
    # smaller — which is exactly the understatement FNOR-001 exists to prevent.
    assert lot_notional(LOT_SIZE, underlying_price=UNDERLYING) != LOT_SIZE * PREMIUM
    assert (
        notional_per_unit(
            InstrumentType.OPTION, traded_price=PREMIUM, underlying_price=UNDERLYING
        )
        * LOT_SIZE
        == Decimal("1800000.00")
    )


def test_cash_and_futures_are_measured_on_the_traded_price():
    assert notional_per_unit(InstrumentType.EQUITY, traded_price=Decimal("250.50")) == Decimal(
        "250.50"
    )
    futures = Decimal("24105.75")
    assert notional_per_unit(InstrumentType.FUTURE, traded_price=futures) == futures
    # A supplied underlying cannot shrink a futures measure below its own price.
    assert (
        notional_per_unit(
            InstrumentType.FUTURE, traded_price=futures, underlying_price=Decimal("24000.00")
        )
        == futures
    )


def test_option_measure_never_reads_below_the_premium_committed():
    """A deep in-the-money put's premium can exceed the spot it is written on."""
    assert notional_per_unit(
        InstrumentType.OPTION, traded_price=Decimal("500.00"), underlying_price=Decimal("300.00")
    ) == Decimal("500.00")


@pytest.mark.parametrize(
    "underlying",
    [None, Decimal(0), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_option_without_an_observed_underlying_fails_closed(underlying):
    """No premium fallback: an unobserved underlying is unavailable, not small."""
    with pytest.raises(ExposureUnavailable) as raised:
        notional_per_unit(
            InstrumentType.OPTION, traded_price=PREMIUM, underlying_price=underlying
        )
    assert raised.value.code == "EXPOSURE_UNDERLYING_UNAVAILABLE"
    with pytest.raises(ExposureUnavailable):
        lot_notional(LOT_SIZE, underlying_price=underlying)


@pytest.mark.parametrize("traded", [None, Decimal(0), Decimal("-1"), Decimal("NaN")])
def test_absent_traded_price_is_unavailable_for_every_instrument(traded):
    for instrument_type in InstrumentType:
        with pytest.raises(ExposureUnavailable) as raised:
            notional_per_unit(
                instrument_type, traded_price=traded, underlying_price=UNDERLYING
            )
        assert raised.value.code == "EXPOSURE_PRICE_UNAVAILABLE"


@pytest.mark.parametrize("lot_size", [0, -1, True, 1.0, "75", None])
def test_lot_size_is_never_guessed(lot_size):
    with pytest.raises(ExposureUnavailable) as raised:
        lot_notional(lot_size, underlying_price=UNDERLYING)
    assert raised.value.code == "EXPOSURE_LOT_SIZE_UNAVAILABLE"


def test_exposure_unavailable_fails_closed_and_is_never_retried():
    assert ExposureUnavailable("x").retryable is False
