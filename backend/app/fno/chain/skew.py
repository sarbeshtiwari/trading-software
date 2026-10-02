"""OC-006: un-interpolated IV curves in percentage points, as in the Greek model.

Missing points stay missing. Term structure uses each expiry's nearest spot
strike (lower strike on ties), not a substitute strike with conveniently present IV.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.fno.chain.model import require_as_of, validate_chain
from app.marketdata.models import OptionChain


class IVPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    strike: Decimal
    call_iv: Decimal | None
    put_iv: Decimal | None
    put_minus_call: Decimal | None


class TermPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expiry: date
    observed_at: datetime
    atm: IVPoint | None


def iv_skew(chain: OptionChain) -> tuple[IVPoint, ...]:
    validate_chain(chain)
    result = []
    for row in sorted(chain.strikes, key=lambda item: item.strike):
        call = row.call.greeks.implied_volatility if row.call and row.call.greeks else None
        put = row.put.greeks.implied_volatility if row.put and row.put.greeks else None
        result.append(
            IVPoint(
                strike=row.strike,
                call_iv=call,
                put_iv=put,
                put_minus_call=put - call if put is not None and call is not None else None,
            )
        )
    return tuple(result)


def term_structure(
    chains: list[OptionChain],
    *,
    as_of: datetime,
    max_age: timedelta,
) -> tuple[TermPoint, ...]:
    if len({(chain.underlying, chain.data_origin) for chain in chains}) > 1:
        raise ValueError("mixed underlying or provenance")
    if len({chain.expiry for chain in chains}) != len(chains):
        raise ValueError("duplicate expiry")
    result = []
    for chain in chains:
        require_as_of(chain, as_of, max_age)
        points = iv_skew(chain)
        atm = (
            min(points, key=lambda point: (abs(point.strike - chain.spot), point.strike))
            if points and chain.spot is not None
            else None
        )
        result.append(TermPoint(expiry=chain.expiry, observed_at=chain.observed_at, atm=atm))
    return tuple(sorted(result, key=lambda point: point.expiry))
