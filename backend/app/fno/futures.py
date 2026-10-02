"""FUT-001: futures identities resolved from supplied master data, not symbol guesses."""

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import IST
from app.core.enums import Exchange, InstrumentType, Segment
from app.db.models.instrument import Instrument
from app.fno.chain.model import aware


class FutureContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    exchange: Literal[Exchange.NSE, Exchange.BSE]
    expiry: date
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0)

    @property
    def expires_at(self) -> datetime:
        return datetime.combine(self.expiry, time(15, 30), tzinfo=IST)


def future_contract(instrument: Instrument) -> FutureContract:
    if (
        instrument.instrument_type != InstrumentType.FUTURE
        or instrument.segment != Segment.FNO
        or instrument.is_active is not True
        or instrument.is_restricted is not False
    ):
        raise ValueError("inactive, restricted or unsupported future")
    return FutureContract(
        symbol=instrument.trading_symbol,
        underlying=instrument.underlying,
        exchange=instrument.exchange,
        expiry=instrument.expiry_date,
        lot_size=instrument.lot_size,
        tick_size=instrument.tick_size,
    )


def resolve_future(
    instruments: Sequence[Instrument],
    underlying: str,
    *,
    exchange: Exchange,
    as_of: datetime,
    tenor: Literal["NEAR", "NEXT", "FAR"] = "NEAR",
) -> FutureContract | None:
    """Resolve actual ordered maturities; a restricted target is unavailable.

    Callers can supply InstrumentResolver.derivatives_for(underlying). Historical
    callers must supply the master known at as_of, not today's mutable master.
    """
    aware(as_of)
    offsets = {"NEAR": 0, "NEXT": 1, "FAR": 2}
    if tenor not in offsets or exchange not in (Exchange.NSE, Exchange.BSE):
        raise ValueError("unsupported tenor or exchange")
    matches = [
        row
        for row in instruments
        if row.underlying == underlying
        and row.exchange == exchange
        and row.instrument_type == InstrumentType.FUTURE
        and row.segment == Segment.FNO
        and row.is_active is True
        and row.expiry_date is not None
        and datetime.combine(row.expiry_date, time(15, 30), tzinfo=IST) > as_of
    ]
    expiries = sorted({row.expiry_date for row in matches})
    offset = offsets[tenor]
    if offset >= len(expiries):
        return None
    candidates = [row for row in matches if row.expiry_date == expiries[offset]]
    if len(candidates) != 1:
        raise ValueError("ambiguous futures contract")
    if candidates[0].is_restricted is not False:
        return None
    return future_contract(candidates[0])
