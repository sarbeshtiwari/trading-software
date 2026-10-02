"""Explicit supported product/segment matrix, not broker connectivity evidence."""

from app.core.enums import Product, Segment
from app.strategies.products import product_blocker


def test_product_contract_matrix():
    expected = (
        (Product.MIS, Segment.CASH, False, None),
        (Product.MIS, Segment.FNO, False, None),
        (Product.CNC, Segment.CASH, False, "POSITIONAL_TRADING_DISABLED"),
        (Product.CNC, Segment.CASH, True, None),
        (Product.NRML, Segment.FNO, False, "POSITIONAL_TRADING_DISABLED"),
        (Product.NRML, Segment.FNO, True, None),
        (Product.CNC, Segment.FNO, True, "PRODUCT_SEGMENT_MISMATCH"),
        (Product.NRML, Segment.CASH, True, "PRODUCT_SEGMENT_MISMATCH"),
        ("UNKNOWN", Segment.CASH, True, "PRODUCT_SEGMENT_MISMATCH"),
        (Product.MIS, "UNKNOWN", True, "PRODUCT_SEGMENT_MISMATCH"),
    )
    for product, segment, allowed, reason in expected:
        assert product_blocker(product, segment, allow_positional=allowed) == reason
