"""OC-001: validate the existing broker-neutral chain without replacing its model."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import TypeAdapter

from app.core.clock import IST
from app.core.enums import OptionType
from app.marketdata.models import OptionChain, OptionLeg, OptionStrike

chain_adapter = TypeAdapter(OptionChain)


def aware(value: datetime) -> datetime:
    """Reject ambiguous timestamps instead of assuming a host timezone."""
    if value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value


def validate_chain(chain: OptionChain) -> OptionChain:
    """Reject malformed ladders; retain explicit missing fields and provenance."""
    aware(chain.observed_at)
    if not chain.underlying.strip():
        raise ValueError("underlying required")
    if chain.spot is not None:
        _number(chain.spot, positive=True)
    seen = set()
    for row in chain.strikes:
        _number(row.strike, positive=True)
        if row.strike in seen:
            raise ValueError("duplicate strike")
        seen.add(row.strike)
        for leg, side in ((row.call, OptionType.CE), (row.put, OptionType.PE)):
            if leg is not None:
                _validate_leg(leg, side, chain.observed_at)
    return chain


def _validate_leg(leg: OptionLeg, side: OptionType, observed_at: datetime) -> None:
    if leg.option_type != side:
        raise ValueError("CE/PE side mismatch")
    for value in (leg.ltp, leg.bid, leg.ask):
        if value is not None:
            _number(value)
    if leg.bid is not None and leg.ask is not None and leg.bid > leg.ask:
        raise ValueError("crossed option quote")
    for value in (leg.volume, leg.open_interest, leg.open_interest_change):
        if value is not None and type(value) is not int:
            raise ValueError("OI and volume must be integers")
    if any(value is not None and value < 0 for value in (leg.volume, leg.open_interest)):
        raise ValueError("negative OI or volume")
    if leg.greeks is not None:
        _validate_greeks(leg, observed_at)


def _validate_greeks(leg: OptionLeg, observed_at: datetime) -> None:
    for name in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility"):
        value = getattr(leg.greeks, name)
        if value is not None and not value.is_finite():
            raise ValueError("non-finite Greek")
    volatility = leg.greeks.implied_volatility
    if volatility is not None and volatility < 0:
        raise ValueError("negative IV")
    if leg.greeks.computed_at is not None:
        if aware(leg.greeks.computed_at) > observed_at:
            raise ValueError("future Greeks")


def _number(value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("finite Decimal required")
    if value < 0 or (positive and value == 0):
        raise ValueError("invalid negative or zero value")


def require_as_of(chain: OptionChain, as_of: datetime, max_age: timedelta) -> None:
    """Decision-time gate, including future observations and expired contracts."""
    validate_chain(chain)
    age = aware(as_of) - chain.observed_at
    if max_age < timedelta(0) or age < timedelta(0) or age > max_age:
        raise ValueError("future or stale option chain")
    if chain.expiry is None:
        raise ValueError("expiry unavailable")
    expiry_close = datetime.combine(chain.expiry, datetime.min.time(), tzinfo=IST).replace(
        hour=15, minute=30
    )
    if as_of >= expiry_close:
        raise ValueError("expired option chain")


def band(chain: OptionChain, lower: Decimal | None, upper: Decimal | None) -> list[OptionStrike]:
    """Inclusive strike band; no nearest-strike substitution."""
    validate_chain(chain)
    if lower is not None:
        _number(lower, positive=True)
    if upper is not None:
        _number(upper, positive=True)
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("reversed strike band")
    return [
        row
        for row in chain.strikes
        if (lower is None or row.strike >= lower) and (upper is None or row.strike <= upper)
    ]


def complete_metric(rows: list[OptionStrike], name: str) -> bool:
    """Totals require both sides at every selected strike, including explicit zeros."""
    return bool(rows) and all(
        leg is not None and getattr(leg, name) is not None
        for row in rows
        for leg in (row.call, row.put)
    )
