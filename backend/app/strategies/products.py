"""Declared MIS means intraday; positional products require explicit owner enablement."""

from app.core.enums import Product, Segment


def product_blocker(product: Product, segment: Segment, *, allow_positional: bool):
    if product not in tuple(Product) or segment not in tuple(Segment):
        return "PRODUCT_SEGMENT_MISMATCH"
    if (product == Product.CNC and segment != Segment.CASH) or (
        product == Product.NRML and segment != Segment.FNO
    ):
        return "PRODUCT_SEGMENT_MISMATCH"
    if product != Product.MIS and not allow_positional:
        return "POSITIONAL_TRADING_DISABLED"
    return None
