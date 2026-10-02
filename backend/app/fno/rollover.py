"""FUT-002: advisory rollover plans; no broker calls and no assumed fills.

The estimate separates spread-crossing cost, supplied estimated fees, and signed
cash outflow between maturities. Futures cash outflow here is a notional price
difference, not an account debit or broker margin requirement.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.clock import IST
from app.core.enums import TransactionType
from app.fno.chain.model import aware
from app.fno.futures import FutureContract


class FutureQuote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    contract: FutureContract
    observed_at: AwareDatetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    volume: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def valid_book(self):
        if self.bid > self.ask:
            raise ValueError("crossed futures book")
        if self.bid % self.contract.tick_size or self.ask % self.contract.tick_size:
            raise ValueError("off-tick futures book")
        return self


class RolloverPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    window_days: int = Field(ge=0, strict=True)
    min_volume: int = Field(ge=0, strict=True)
    max_spread: Decimal = Field(ge=0)
    max_age_seconds: int = Field(gt=0, strict=True)
    max_quote_skew_seconds: int = Field(ge=0, strict=True)


class RolloverLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    contract: FutureContract
    side: TransactionType
    quantity: int = Field(gt=0, strict=True)
    reference_price: Decimal


class RolloverPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    as_of: AwareDatetime
    exit: RolloverLeg
    entry: RolloverLeg
    spread_cost: Decimal
    fees: Decimal | None
    total_estimated_cost: Decimal | None
    notional_price_difference: Decimal
    estimate: Literal["ESTIMATED"] = "ESTIMATED"


def rollover_plan(
    near: FutureQuote,
    far: FutureQuote,
    *,
    signed_quantity: int,
    as_of: datetime,
    policy: RolloverPolicy,
    estimated_fees: Decimal | None = None,
) -> RolloverPlan | None:
    aware(as_of)
    _validate_pair(near, far, as_of, policy)
    if type(signed_quantity) is not int or signed_quantity == 0:
        raise ValueError("nonzero integer position required")
    quantity = abs(signed_quantity)
    if quantity % near.contract.lot_size or quantity % far.contract.lot_size:
        raise ValueError("rollover quantity incompatible with contract lot sizes")
    if estimated_fees is not None and (not estimated_fees.is_finite() or estimated_fees < 0):
        raise ValueError("invalid estimated fees")
    days = (near.contract.expiry - as_of.astimezone(IST).date()).days
    if days > policy.window_days:
        return None
    long_position = signed_quantity > 0
    exit_price = near.bid if long_position else near.ask
    entry_price = far.ask if long_position else far.bid
    spread_cost = ((near.ask - near.bid) + (far.ask - far.bid)) * quantity / 2
    return RolloverPlan(
        as_of=as_of,
        exit=RolloverLeg(
            contract=near.contract,
            quantity=quantity,
            reference_price=exit_price,
            side=TransactionType.SELL if long_position else TransactionType.BUY,
        ),
        entry=RolloverLeg(
            contract=far.contract,
            quantity=quantity,
            reference_price=entry_price,
            side=TransactionType.BUY if long_position else TransactionType.SELL,
        ),
        spread_cost=spread_cost,
        fees=estimated_fees,
        total_estimated_cost=spread_cost + estimated_fees if estimated_fees is not None else None,
        notional_price_difference=(entry_price - exit_price) * signed_quantity,
    )


def _validate_pair(near, far, as_of, policy):
    if (near.contract.underlying, near.contract.exchange) != (
        far.contract.underlying,
        far.contract.exchange,
    ) or near.contract.expiry >= far.contract.expiry:
        raise ValueError("incompatible rollover contracts")
    if abs(near.observed_at - far.observed_at) > timedelta(seconds=policy.max_quote_skew_seconds):
        raise ValueError("unsynchronised rollover quotes")
    for quote in (near, far):
        if as_of >= quote.contract.expires_at:
            raise ValueError("expired futures contract")
        if (
            not timedelta(0)
            <= as_of - quote.observed_at
            <= timedelta(seconds=policy.max_age_seconds)
        ):
            raise ValueError("future or stale rollover quote")
        if quote.volume < policy.min_volume or quote.ask - quote.bid > policy.max_spread:
            raise ValueError("insufficient futures liquidity")
