"""EXCH-004: price and size must be on the exchange's grid before dispatch.

Tick 0.05 and lot 75 are fixture values supplied the way the instrument master
supplies them; nothing here asserts a real NSE tick or lot size as fact.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.brokers.models import OrderRequest
from app.core.enums import Exchange, OrderType, Product, Segment, TransactionType
from app.core.money import round_to_tick
from app.execution.compliance import ExchangeComplianceError, enforce

TICK = Decimal("0.05")
LOT = 75


def request(**changes):
    values = {
        "trading_symbol": "FIXTURE",
        "exchange": Exchange.NSE,
        "segment": Segment.FNO,
        "product": Product.MIS,
        "order_type": OrderType.LIMIT,
        "transaction_type": TransactionType.BUY,
        "quantity": LOT,
        "reference_id": "ref-0000000000001",
        "price": Decimal("100.05"),
    }
    return OrderRequest(**(values | changes))


def test_tick_lot_compliance():
    """The acceptance case: compliant passes, non-compliant is corrected or rejected."""
    enforce(request(), tick_size=TICK, lot_size=LOT)

    # Rejected: an off-tick price never reaches the broker.
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(request(price=Decimal("100.03")), tick_size=TICK, lot_size=LOT)
    assert raised.value.code == "EXCH_PRICE_OFF_TICK"

    # Rejected: a quantity that is not a whole number of lots, in F&O.
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(request(quantity=LOT + 1), tick_size=TICK, lot_size=LOT)
    assert raised.value.code == "EXCH_QUANTITY_NOT_LOT_MULTIPLE"

    # Corrected upstream: a buy limit rounds down onto the grid, never paying
    # more, and the corrected price then passes this gate.
    corrected = round_to_tick(Decimal("100.03"), TICK, direction="down")
    assert corrected == Decimal("100.00")
    enforce(request(price=corrected), tick_size=TICK, lot_size=LOT)


def test_correction_direction_never_worsens_the_order():
    """A sell rounds up, a buy rounds down; both land on the grid."""
    for direction, expected in (("down", Decimal("100.00")), ("up", Decimal("100.05"))):
        price = round_to_tick(Decimal("100.03"), TICK, direction=direction)
        assert price == expected
        assert price % TICK == 0
    # An already-compliant price is left exactly where it is, either way.
    for direction in ("down", "up"):
        assert round_to_tick(Decimal("100.05"), TICK, direction=direction) == Decimal("100.05")


def test_cash_quantity_is_not_held_to_a_lot_multiple():
    """Lot multiples are an F&O rule; a single cash share is a valid order."""
    enforce(
        request(segment=Segment.CASH, quantity=1, price=Decimal("250.05")),
        tick_size=TICK,
        lot_size=LOT,
    )


def test_trigger_prices_are_rejected_rather_than_quietly_moved():
    """This layer cannot know which direction keeps a protective trigger safe."""
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(
            request(order_type=OrderType.STOP_LOSS, trigger_price=Decimal("96.03")),
            tick_size=TICK,
            lot_size=LOT,
        )
    assert raised.value.code == "EXCH_TRIGGER_OFF_TICK"
    enforce(
        request(order_type=OrderType.STOP_LOSS, trigger_price=Decimal("96.05")),
        tick_size=TICK,
        lot_size=LOT,
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("price", Decimal(0), "EXCH_PRICE_OFF_TICK"),
        ("price", Decimal("-100.05"), "EXCH_PRICE_OFF_TICK"),
        ("price", Decimal("NaN"), "EXCH_PRICE_OFF_TICK"),
        ("quantity", 0, "EXCH_QUANTITY_INVALID"),
        ("quantity", -75, "EXCH_QUANTITY_INVALID"),
    ],
)
def test_unusable_price_or_quantity_is_refused(field, value, code):
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(request(**{field: value}), tick_size=TICK, lot_size=LOT)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("tick", "lot", "code"),
    [
        (Decimal(0), LOT, "EXCH_TICK_SIZE_UNAVAILABLE"),
        (Decimal("-0.05"), LOT, "EXCH_TICK_SIZE_UNAVAILABLE"),
        (Decimal("NaN"), LOT, "EXCH_TICK_SIZE_UNAVAILABLE"),
        (None, LOT, "EXCH_TICK_SIZE_UNAVAILABLE"),
        (TICK, 0, "EXCH_LOT_SIZE_UNAVAILABLE"),
        (TICK, -75, "EXCH_LOT_SIZE_UNAVAILABLE"),
        (TICK, True, "EXCH_LOT_SIZE_UNAVAILABLE"),
        (TICK, 75.0, "EXCH_LOT_SIZE_UNAVAILABLE"),
    ],
)
def test_contract_specification_is_never_guessed(tick, lot, code):
    """Missing master data fails closed instead of defaulting to a grid."""
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(request(), tick_size=tick, lot_size=lot)
    assert raised.value.code == code


def test_a_market_order_without_a_price_is_still_size_checked():
    enforce(
        replace(request(), order_type=OrderType.MARKET, price=None),
        tick_size=TICK,
        lot_size=LOT,
    )
    with pytest.raises(ExchangeComplianceError) as raised:
        enforce(
            replace(request(), order_type=OrderType.MARKET, price=None, quantity=LOT + 1),
            tick_size=TICK,
            lot_size=LOT,
        )
    assert raised.value.code == "EXCH_QUANTITY_NOT_LOT_MULTIPLE"


def test_compliance_failure_fails_closed_and_is_never_retried():
    assert ExchangeComplianceError("x").retryable is False
