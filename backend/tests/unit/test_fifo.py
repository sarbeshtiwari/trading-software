"""Independent hand arithmetic, including the requested scale-in/out example."""

from decimal import Decimal

import pytest

from app.brokers.paper.account import PaperAccount
from app.core.enums import Product, Segment, TransactionType
from app.core.errors import ValidationError
from app.portfolio.fifo import Lot, match_fill


def test_scale_in_partial_exit_and_flatten():
    first = match_fill((), Lot(100, Decimal(100), "buy-1"))
    second = match_fill(first.lots, Lot(100, Decimal(110), "buy-2"))
    partial = match_fill(second.lots, Lot(-50, Decimal(120), "sell-1"))
    assert partial.realised == 1000
    assert [(lot.source_id, lot.quantity) for lot in partial.lots] == [
        ("buy-1", 50),
        ("buy-2", 100),
    ]
    assert partial.average_price == Decimal(16000) / 150
    final = match_fill(partial.lots, Lot(-150, Decimal(125), "sell-2"))
    assert final.realised == 2750
    assert partial.realised + final.realised == 3750
    assert final.quantity == 0 and final.average_price == 0
    assert [(item.entry_id, item.quantity) for item in final.matches] == [
        ("buy-1", 50),
        ("buy-2", 100),
    ]


def test_short_lots_and_reversal_keep_exact_unrounded_profit():
    first = match_fill((), Lot(-25, Decimal("101.005"), "short-1"))
    second = match_fill(first.lots, Lot(-25, Decimal("102.005"), "short-2"))
    reversal = match_fill(second.lots, Lot(60, Decimal(100), "cover"))
    assert reversal.realised == Decimal("75.250")
    assert reversal.lots == (Lot(10, Decimal(100), "cover"),)


@pytest.mark.parametrize(
    "quantity,price", [(0, "10"), (True, "10"), (1, "NaN"), (1, "0"), (1, "-1")]
)
def test_invalid_lots(quantity, price):
    with pytest.raises(ValueError):
        Lot(quantity, Decimal(price), "test")


@pytest.mark.parametrize("corruption", ["missing_lots", "quantity", "average", "realised"])
def test_account_restore_refuses_legacy_or_corrupt_fifo(corruption):
    account = PaperAccount(Decimal(10000))
    account.apply_fill(
        trading_symbol="FIXTURE",
        segment=Segment.CASH,
        product=Product.MIS,
        transaction_type=TransactionType.BUY,
        quantity=100,
        price=Decimal(10),
    )
    snapshot = account.to_dict()
    row = snapshot["positions"][0]
    if corruption == "missing_lots":
        del row["fifo_lots"]
    elif corruption == "quantity":
        row["fifo_lots"][0]["quantity"] = 99
    elif corruption == "average":
        row["average_price"] = "11"
    else:
        row["realised_exact"] = "1"
    with pytest.raises(ValidationError, match="FIFO"):
        PaperAccount.from_dict(snapshot)


def test_account_fractional_profit_rounds_cumulatively_across_restart():
    account = PaperAccount(Decimal(10000))
    common = {"trading_symbol": "FIXTURE", "segment": Segment.CASH, "product": Product.MIS}
    account.apply_fill(
        **common, transaction_type=TransactionType.BUY, quantity=2, price=Decimal("10.005")
    )
    account.apply_fill(
        **common, transaction_type=TransactionType.SELL, quantity=1, price=Decimal("10.01")
    )
    restored = PaperAccount.from_dict(account.to_dict())
    restored.apply_fill(
        **common, transaction_type=TransactionType.SELL, quantity=1, price=Decimal("10.01")
    )
    assert restored.realised_pnl == Decimal("0.01")
    assert restored.cash == Decimal("10000.01")


def test_opposing_open_lots_fail_closed():
    with pytest.raises(ValueError, match="opposing"):
        match_fill(
            (Lot(1, Decimal(10), "one"), Lot(-1, Decimal(10), "two")), Lot(1, Decimal(10), "new")
        )
