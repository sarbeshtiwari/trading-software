"""Checks supplied exchange bands without deriving or inventing missing limits."""

from decimal import Decimal


def circuit_status(quote, prices):
    lower, upper = quote.lower_circuit, quote.upper_circuit
    if lower is None and upper is None:
        return "UNAVAILABLE"
    if (
        not isinstance(lower, Decimal)
        or not isinstance(upper, Decimal)
        or not lower.is_finite()
        or not upper.is_finite()
        or lower <= 0
        or upper < lower
    ):
        return "INVALID_CIRCUIT_BAND"
    if any(not price.is_finite() or price < lower or price > upper for price in prices):
        return "PRICE_OUTSIDE_CIRCUIT_BAND"
    return "AVAILABLE"
