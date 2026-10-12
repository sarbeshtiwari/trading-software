"""TEST-005/PNL-002: unrealised P&L and equity, hand-computed.

Realised FIFO, costs, reversals and restart rounding are covered in
`tests/unit/test_fifo.py` and `test_costs.py`. What had no unit-level coverage
was the mark-to-market half: unrealised P&L across long and short sides, F&O lot
multiples, and the point that an unmarked position contributes nothing rather
than being valued at its own entry basis.

Every expectation below is arithmetic stated in the assertion.
"""

from decimal import Decimal

import pytest

from app.brokers.paper.account import PaperAccount, PaperPosition
from app.core.enums import InstrumentType, Product, Segment
from app.core.errors import ValidationError


def position(**changes):
    values = {
        "trading_symbol": "FIXTURE",
        "segment": Segment.CASH,
        "product": Product.MIS,
        "net_quantity": 100,
        "average_price": Decimal("250.00"),
        "last_price": Decimal("252.50"),
    }
    return PaperPosition(**(values | changes))


def account(*positions, capital=Decimal(100000)):
    held = PaperAccount(starting_capital=capital)
    for item in positions:
        held.positions[item.key] = item
    return held


def test_pnl_unrealised_long_and_short_use_the_signed_quantity():
    #  long 100 at 250.00 marked 252.50 -> +2.50 x 100 = +250.00
    assert account(position()).unrealised_pnl() == Decimal("250.00")
    #  the same move against a long is a loss of the same size
    assert account(position(last_price=Decimal("247.50"))).unrealised_pnl() == Decimal("-250.00")
    #  short 100 at 250.00 marked 247.50 -> a fall is a gain: -2.50 x -100
    short = position(net_quantity=-100, last_price=Decimal("247.50"))
    assert account(short).unrealised_pnl() == Decimal("250.00")
    #  and a rise is a loss on the short side
    assert account(
        position(net_quantity=-100, last_price=Decimal("252.50"))
    ).unrealised_pnl() == Decimal("-250.00")


def test_pnl_unrealised_sums_across_offsetting_positions():
    """Two positions nearly cancelling must net, not take the larger side."""
    held = account(
        position(trading_symbol="ONE", last_price=Decimal("252.50")),
        position(trading_symbol="TWO", last_price=Decimal("249.00")),
    )
    #  +250.00 from ONE, -100.00 from TWO (-1.00 x 100)
    assert held.unrealised_pnl() == Decimal("150.00")


def test_pnl_unrealised_scales_with_the_fno_lot_multiple():
    """An option is marked per unit, so 4 lots of 75 is 300 units of premium."""
    lots, lot_size = 4, 75
    held = account(
        position(
            segment=Segment.FNO,
            instrument_type=InstrumentType.OPTION,
            net_quantity=lots * lot_size,
            average_price=Decimal("101.00"),
            last_price=Decimal("103.50"),
        )
    )
    #  +2.50 of premium on each of 300 units = +750.00
    assert held.unrealised_pnl() == Decimal("750.00")
    assert lots * lot_size == 300


def test_pnl_unmarked_position_contributes_nothing_rather_than_its_entry():
    """An unmarked position is not worth its own cost basis; it is unvalued."""
    held = account(position(last_price=None))
    assert held.unrealised_pnl() == Decimal("0.00")
    #  A flat position is likewise excluded even when a mark exists.
    assert account(position(net_quantity=0)).unrealised_pnl() == Decimal("0.00")


def test_pnl_equity_is_cash_plus_unrealised_and_never_accumulates():
    """Equity must track the latest mark, not the sum of every mark seen."""
    held = account(position())
    assert held.cash == Decimal(100000)
    assert held.equity == Decimal("100250.00")
    #  Re-marking higher replaces the unrealised figure rather than adding to it:
    #  +5.00 x 100 = +500.00 against unchanged cash, not 250.00 + 500.00.
    held.mark("FIXTURE", Segment.CASH, Product.MIS, Decimal("255.00"))
    assert held.unrealised_pnl() == Decimal("500.00")
    assert held.equity == Decimal("100500.00")
    assert held.cash == Decimal(100000)


def test_pnl_unrealised_quantises_to_paise_without_drifting():
    """A third-of-a-paisa move per unit is rounded once, at the total."""
    held = account(
        position(net_quantity=3, average_price=Decimal("100.00"), last_price=Decimal("100.001"))
    )
    #  0.001 x 3 = 0.003 -> 0.00 at paise, not 3 separate roundings of 0.00
    assert held.unrealised_pnl() == Decimal("0.00")
    larger = account(
        position(net_quantity=3000, average_price=Decimal("100.00"), last_price=Decimal("100.001"))
    )
    #  0.001 x 3000 = 3.00 exactly
    assert larger.unrealised_pnl() == Decimal("3.00")


@pytest.mark.parametrize("capital", [Decimal(0), Decimal("-1")])
def test_pnl_account_requires_real_starting_capital(capital):
    """Starting capital is never invented or defaulted to zero."""
    with pytest.raises(ValidationError, match="starting capital must be positive"):
        PaperAccount(starting_capital=capital)
