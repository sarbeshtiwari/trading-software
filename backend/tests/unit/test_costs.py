"""Hand-computed synthetic tariffs, not claims about current broker billing."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.enums import Exchange, Product, Segment, TransactionType
from app.portfolio.costs import FeeSchedule, order_costs
from tests.unit.test_option_chain import OBSERVED


def schedule(**changes):
    values = {
        "version": "isolated-1",
        "source": "deterministic-test-fixture",
        "known_at": OBSERVED,
        "effective_from": OBSERVED,
        "effective_to": OBSERVED + timedelta(days=1),
        "exchange": Exchange.NSE,
        "segment": Segment.CASH,
        "product": Product.MIS,
        "brokerage_rate": ".001",
        "brokerage_minimum": 5,
        "brokerage_maximum": 20,
        "small_trade_brokerage_cap_rate": ".025",
        "stt_buy_rate": 0,
        "stt_sell_rate": ".00025",
        "exchange_rate": ".00003",
        "sebi_rate": ".000001",
        "ipft_rate": ".000001",
        "stamp_buy_rate": ".00003",
        "stamp_sell_rate": 0,
        "gst_rate": ".18",
        "money_quantum": ".01",
        "stt_quantum": ".01",
    }
    return FeeSchedule(**(values | changes))


def test_explicit_component_charges_and_side_dependence():
    buy = order_costs(schedule(), Decimal(10000), TransactionType.BUY, as_of=OBSERVED)
    assert buy == {
        "brokerage": Decimal(10),
        "stt": Decimal(0),
        "exchange": Decimal(".30"),
        "sebi": Decimal(".01"),
        "ipft": Decimal(".01"),
        "stamp": Decimal(".30"),
        "gst": Decimal("1.86"),
        "total": Decimal("12.48"),
    }
    sell = order_costs(schedule(), Decimal(10000), TransactionType.SELL, as_of=OBSERVED)
    assert sell["stamp"] == 0 and sell["stt"] == Decimal("2.50")
    assert sell["gst"] == Decimal("1.86") and sell["total"] == Decimal("14.68")


def test_fee_boundaries_do_not_bill_empty_orders_or_multiply_brokerage_cap():
    assert order_costs(schedule(), Decimal(0), "BUY", as_of=OBSERVED)["total"] == 0
    assert order_costs(schedule(), Decimal(5), "BUY", as_of=OBSERVED)["total"] == Decimal(".15")
    assert order_costs(schedule(), Decimal(100000), "BUY", as_of=OBSERVED)["brokerage"] == 20


@pytest.mark.parametrize(
    "change",
    [
        {"known_at": OBSERVED + timedelta(seconds=1)},
        {"effective_from": OBSERVED + timedelta(seconds=1)},
        {"effective_from": OBSERVED - timedelta(days=1), "effective_to": OBSERVED},
    ],
)
def test_fee_knowledge_and_effective_dates_prevent_lookahead(change):
    with pytest.raises(ValueError, match="unavailable"):
        order_costs(schedule(**change), Decimal(10000), "BUY", as_of=OBSERVED)


@pytest.mark.parametrize("value", [Decimal(-1), Decimal("NaN"), Decimal("Infinity"), 1.5])
def test_invalid_turnover_fails_closed(value):
    with pytest.raises(ValueError):
        order_costs(schedule(), value, "BUY", as_of=OBSERVED)


def test_option_fees_require_explicit_premium_basis():
    with pytest.raises(ValueError, match="charge basis"):
        schedule(segment=Segment.FNO)
    with pytest.raises(ValueError, match="charge basis"):
        schedule(charge_basis="OPTION_PREMIUM")
    tariff = schedule(segment=Segment.FNO, charge_basis="OPTION_PREMIUM")
    charges = order_costs(tariff, Decimal(10000), "SELL", as_of=OBSERVED)
    assert charges["total"] == Decimal("14.68")
    assert charges["brokerage"] == Decimal(10)
    assert charges["stt"] == Decimal("2.50")
