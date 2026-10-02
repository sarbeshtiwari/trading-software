"""PAPER must not project an exchange that its stored execution evidence did not supply."""

from dataclasses import replace

import pytest

from app.core.enums import Exchange
from app.core.errors import ValidationError
from tests.integration.test_paper_broker import _quote, _request, paper

__all__ = ["paper"]


async def test_unsupported_bse_order_is_rejected_without_execution(paper):
    with pytest.raises(ValidationError, match="exchange BSE"):
        await paper.place_order(_request(exchange=Exchange.BSE))
    assert await paper.list_orders() == []


async def test_position_exchange_comes_from_fill_lineage_not_constant(paper):
    async def quote(instrument):
        return _quote("100", asks=(("100", 1000),), bids=(("99.95", 1000),))

    paper._quote_source = quote
    await paper.place_order(_request())
    assert (await paper.get_positions())[0].exchange == Exchange.NSE
    order = next(iter(paper._orders.values()))
    order.request = replace(order.request, exchange=Exchange.BSE)
    assert (await paper.get_positions())[0].exchange == Exchange.BSE
    order.filled_quantity = 0
    with pytest.raises(ValidationError, match="exchange lineage"):
        await paper.get_positions()
