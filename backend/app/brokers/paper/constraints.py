"""Persisted instrument constraints for simulated execution, never strategy inputs."""

from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.core.enums import InstrumentType, OptionType
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.fno.greeks.time import expiry_moment


class FillConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_key: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    captured_at: AwareDatetime
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0, allow_inf_nan=False)
    instrument_type: InstrumentType | None = None
    expiry_date: date | None = None
    option_type: OptionType | None = None
    strike_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    underlying: str | None = None

    def option_blocker(self, as_of):
        if (
            not self.expiry_date
            or not self.option_type
            or not self.strike_price
            or not self.underlying
        ):
            return "OPTION_CONTRACT_UNAVAILABLE"
        if as_of >= expiry_moment(self.expiry_date):
            return "OPTION_CONTRACT_EXPIRED"
        return None

    @field_validator("tick_size")
    @classmethod
    def supported_precision(cls, value):
        if value.as_tuple().exponent < -8:
            raise ValueError("tick precision exceeds persisted price precision")
        return value

    def on_tick(self, price):
        return price.is_finite() and price > 0 and price % self.tick_size == 0

    def adverse_price(self, price, *, buying):
        rounding = ROUND_CEILING if buying else ROUND_FLOOR
        return (price / self.tick_size).to_integral_value(rounding=rounding) * self.tick_size


async def database_constraints(request, as_of):
    async with db_session.session_scope() as session:
        instrument = await session.scalar(
            sa.select(Instrument).where(
                Instrument.exchange == request.exchange,
                Instrument.segment == request.segment,
                Instrument.trading_symbol == request.trading_symbol,
            )
        )
    if instrument is None:
        return None
    return FillConstraints(
        instrument_key=instrument.key,
        instrument_id=instrument.id,
        captured_at=as_of,
        lot_size=instrument.lot_size,
        tick_size=instrument.tick_size,
        instrument_type=instrument.instrument_type,
        expiry_date=instrument.expiry_date,
        option_type=instrument.option_type,
        strike_price=instrument.strike_price,
        underlying=instrument.underlying,
    )
