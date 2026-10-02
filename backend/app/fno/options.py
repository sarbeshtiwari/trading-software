"""OPT-001: validated option identities and exact Decimal moneyness."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.db.models.instrument import Instrument


class OptionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    exchange: Exchange
    expiry: date
    strike: Decimal = Field(gt=0)
    option_type: OptionType
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0)
    weekly: bool | None = None


def option_contract(instrument: Instrument) -> OptionContract:
    """A listed instrument alone is not evidence of eligibility or liquidity."""
    if (
        instrument.instrument_type != InstrumentType.OPTION
        or instrument.segment != Segment.FNO
        or instrument.exchange not in (Exchange.NSE, Exchange.BSE)
        or instrument.is_active is not True
        or instrument.is_restricted is not False
    ):
        raise ValueError("inactive, restricted or unsupported option instrument")
    return OptionContract(
        symbol=instrument.trading_symbol,
        underlying=instrument.underlying,
        exchange=instrument.exchange,
        expiry=instrument.expiry_date,
        strike=instrument.strike_price,
        option_type=instrument.option_type,
        lot_size=instrument.lot_size,
        tick_size=instrument.tick_size,
        weekly=instrument.is_weekly_expiry,
    )


def classify(contract: OptionContract, spot: Decimal) -> Literal["ATM", "ITM", "OTM"]:
    if not spot.is_finite() or spot <= 0:
        raise ValueError("positive finite spot required")
    if spot == contract.strike:
        return "ATM"
    intrinsic = (
        spot - contract.strike if contract.option_type == OptionType.CE else contract.strike - spot
    )
    return "ITM" if intrinsic > 0 else "OTM"
