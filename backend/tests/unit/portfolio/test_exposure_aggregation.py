"""PORT-004/TEST-010: gross, net and per-dimension exposure, hand-computed.

The book below is deliberately mixed — two instruments in one sector, two
underlyings, long and short — so gross, net and each concentration dimension
come out to different numbers. A measure that silently stood in for another
would have to break one of these literals.

    BANK/HDFC   long    40000   underlying BANKNIFTY
    BANK/ICICI  short   25000   underlying BANKNIFTY
    IT/INFY     long    10000   underlying INFY

    gross                       40000 + 25000 + 10000 = 75000
    net                         40000 - 25000 + 10000 = 25000
    sector BANK                 40000 + 25000         = 65000
    underlying BANKNIFTY        40000 + 25000         = 65000
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.data_origin import DataOrigin
from app.core.enums import SignalDirection
from app.risk.models import Exposure, PortfolioState

MOMENT = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)


def exposure(instrument, sector, underlying, notional, direction):
    return Exposure(
        instrument_id=instrument,
        sector=sector,
        underlying=underlying,
        notional=Decimal(notional),
        direction=direction,
    )


BOOK = (
    exposure("HDFC", "BANK", "BANKNIFTY", 40000, SignalDirection.LONG),
    exposure("ICICI", "BANK", "BANKNIFTY", 25000, SignalDirection.SHORT),
    exposure("INFY", "IT", "INFY", 10000, SignalDirection.LONG),
)


def portfolio(exposures=BOOK):
    return PortfolioState(
        observed_at=MOMENT,
        available_at=MOMENT,
        source_id="test-aggregation",
        data_origin=DataOrigin.SYNTHETIC,
        equity=Decimal(100000),
        peak_equity=Decimal(100000),
        realised_day_pnl=Decimal(0),
        unrealised_day_pnl=Decimal(0),
        available_margin=Decimal(50000),
        reserved_risk=Decimal(0),
        exposures=exposures,
        open_and_pending_positions=len(exposures),
        strategy_open_and_pending_positions=len(exposures),
        strategy_id="test-strategy",
        entries_blocked=False,
        drawdown_disarmed=False,
    )


def test_gross_and_net_exposure_are_different_measures():
    account = portfolio()
    assert account.gross_exposure == Decimal(75000)
    assert account.net_exposure == Decimal(25000)
    assert account.gross_exposure != account.net_exposure


def test_concentration_dimensions_aggregate_independently():
    account = portfolio()
    assert account.exposure_for("sector", "BANK") == Decimal(65000)
    assert account.exposure_for("sector", "IT") == Decimal(10000)
    assert account.exposure_for("underlying", "BANKNIFTY") == Decimal(65000)
    assert account.exposure_for("underlying", "INFY") == Decimal(10000)
    assert account.exposure_for("instrument_id", "HDFC") == Decimal(40000)
    assert account.exposure_for("instrument_id", "ICICI") == Decimal(25000)
    # A dimension nothing is held on carries no exposure, rather than failing.
    assert account.exposure_for("sector", "PHARMA") == Decimal(0)


def test_concentration_never_exceeds_gross_on_any_dimension():
    account = portfolio()
    for attribute in ("instrument_id", "sector", "underlying"):
        keys = {getattr(item, attribute) for item in account.exposures}
        assert sum(account.exposure_for(attribute, key) for key in keys) == account.gross_exposure
        for key in keys:
            assert account.exposure_for(attribute, key) <= account.gross_exposure


def test_a_short_never_reduces_gross_exposure_or_capacity():
    """Offsetting is a net concept; it must not buy room under a gross limit."""
    long_only = portfolio(BOOK[:1])
    hedged = portfolio(BOOK[:2])
    assert hedged.gross_exposure == Decimal(65000) > long_only.gross_exposure
    assert hedged.net_exposure == Decimal(15000) < long_only.net_exposure


def test_fully_offsetting_book_is_flat_net_but_not_flat_gross():
    offset = (
        exposure("HDFC", "BANK", "BANKNIFTY", 40000, SignalDirection.LONG),
        exposure("HDFC", "BANK", "BANKNIFTY", 40000, SignalDirection.SHORT),
    )
    account = portfolio(offset)
    assert account.net_exposure == Decimal(0)
    assert account.gross_exposure == Decimal(80000)


def test_empty_book_has_no_exposure_on_either_measure():
    account = portfolio(())
    assert account.gross_exposure == Decimal(0)
    assert account.net_exposure == Decimal(0)
    assert account.exposure_for("sector", "BANK") == Decimal(0)


def test_notional_stays_unsigned_so_the_sign_lives_in_direction():
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        exposure("HDFC", "BANK", "BANKNIFTY", -1, SignalDirection.LONG)
    item = exposure("HDFC", "BANK", "BANKNIFTY", 40000, SignalDirection.SHORT)
    assert item.notional == Decimal(40000)
    assert item.signed_notional == Decimal(-40000)


@pytest.mark.parametrize("direction", [SignalDirection.EXIT, SignalDirection.NONE])
def test_an_exposure_must_name_a_tradeable_side(direction):
    """EXIT and NONE are not sides a position can be held on."""
    with pytest.raises(ValueError):
        exposure("HDFC", "BANK", "BANKNIFTY", 40000, direction)
