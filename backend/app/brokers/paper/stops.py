"""Durable evidence for a simulated stop's one-way activation."""

from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.data_origin import DataOrigin
from app.core.enums import TransactionType
from app.core.errors import ValidationError


class StopActivation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_key: str = Field(min_length=1)
    activated_at: AwareDatetime
    observed_at: AwareDatetime
    data_origin: DataOrigin
    transaction_type: TransactionType
    ltp: Decimal = Field(gt=0, allow_inf_nan=False)
    trigger_price: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_crossing(self):
        crossed = (
            self.ltp >= self.trigger_price
            if self.transaction_type == TransactionType.BUY
            else self.ltp <= self.trigger_price
        )
        if not crossed or self.observed_at > self.activated_at:
            raise ValueError("invalid stop activation evidence")
        return self

    def require_for(self, request, now):
        key = f"{request.exchange.value}_{request.segment.value}_{request.trading_symbol}"
        if (
            key != self.instrument_key
            or request.transaction_type != self.transaction_type
            or request.trigger_price != self.trigger_price
            or self.activated_at > now
        ):
            raise ValidationError("PAPER stop activation identity or time mismatch")
