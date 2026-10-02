"""Boundary fixtures never stand in for verified exchange price bands."""

from decimal import Decimal

from app.brokers.paper.engine import FillConfig, FillEngine
from app.core.enums import Exchange, OrderStatus, OrderType, Segment, TransactionType
from app.marketdata.models import InstrumentRef, Quote
from tests.unit.test_option_chain import OBSERVED


def test_market_slippage_cannot_cross_supplied_band():
    quote = Quote(
        InstrumentRef("TEST", Exchange.NSE, Segment.CASH),
        Decimal("100"),
        OBSERVED,
        lower_circuit=Decimal("90"),
        upper_circuit=Decimal("100"),
    )
    result = FillEngine(FillConfig(use_depth=False, slippage_bps=Decimal("10"))).simulate(
        order_type=OrderType.MARKET,
        transaction_type=TransactionType.BUY,
        quantity=1,
        limit_price=None,
        trigger_price=None,
        quote=quote,
    )
    assert result.status == OrderStatus.REJECTED
    assert result.reason == "PRICE_OUTSIDE_CIRCUIT_BAND"
    assert result.fills == []
