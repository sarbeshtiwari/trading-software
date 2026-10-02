"""Explicit versioned trading fee estimates; no built-in or inferred tariff rates."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.enums import Exchange, Product, Segment, TransactionType


class FeeSchedule(EvidenceModel):
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    effective_from: AwareDatetime
    effective_to: AwareDatetime
    exchange: Exchange
    segment: Literal[Segment.CASH, Segment.FNO]
    product: Literal[Product.MIS]
    charge_basis: Literal["CASH_TURNOVER", "OPTION_PREMIUM"] = "CASH_TURNOVER"
    brokerage_rate: Decimal = Field(ge=0, le=1)
    brokerage_minimum: Decimal = Field(ge=0)
    brokerage_maximum: Decimal = Field(ge=0)
    small_trade_brokerage_cap_rate: Decimal = Field(ge=0, le=1)
    stt_buy_rate: Decimal = Field(ge=0, le=1)
    stt_sell_rate: Decimal = Field(ge=0, le=1)
    exchange_rate: Decimal = Field(ge=0, le=1)
    sebi_rate: Decimal = Field(ge=0, le=1)
    ipft_rate: Decimal = Field(ge=0, le=1)
    stamp_buy_rate: Decimal = Field(ge=0, le=1)
    stamp_sell_rate: Decimal = Field(ge=0, le=1)
    gst_rate: Decimal = Field(ge=0, le=1)
    money_quantum: Decimal = Field(gt=0, le=1)
    stt_quantum: Decimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def coherent(self):
        if (self.segment == Segment.CASH) != (self.charge_basis == "CASH_TURNOVER"):
            raise ValueError("fee segment and charge basis disagree")
        if self.effective_from >= self.effective_to:
            raise ValueError("fee effective window is empty")
        if self.brokerage_minimum > self.brokerage_maximum:
            raise ValueError("brokerage minimum exceeds maximum")
        return self

    def require_at(self, as_of: datetime):
        if as_of.utcoffset() is None or not (
            self.known_at <= as_of and self.effective_from <= as_of < self.effective_to
        ):
            raise ValueError("fee schedule unavailable at fill time")


def order_costs(schedule: FeeSchedule, turnover: Decimal, side: TransactionType, *, as_of):
    schedule = FeeSchedule.model_validate(schedule.model_dump())
    schedule.require_at(as_of)
    if not isinstance(turnover, Decimal) or not turnover.is_finite() or turnover < 0:
        raise ValueError("finite nonnegative Decimal turnover required")
    side = TransactionType(side)
    minimum = min(schedule.brokerage_minimum, turnover * schedule.small_trade_brokerage_cap_rate)
    brokerage = min(schedule.brokerage_maximum, max(minimum, turnover * schedule.brokerage_rate))
    buying = side == TransactionType.BUY
    raw = {
        "brokerage": brokerage,
        "stt": turnover * (schedule.stt_buy_rate if buying else schedule.stt_sell_rate),
        "exchange": turnover * schedule.exchange_rate,
        "sebi": turnover * schedule.sebi_rate,
        "ipft": turnover * schedule.ipft_rate,
        "stamp": turnover * (schedule.stamp_buy_rate if buying else schedule.stamp_sell_rate),
    }
    rounded = {}
    for name, amount in raw.items():
        quantum = schedule.stt_quantum if name == "stt" else schedule.money_quantum
        rounded[name] = (amount / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum
    taxable = sum((rounded[name] for name in ("brokerage", "exchange", "sebi", "ipft")), Decimal(0))
    rounded["gst"] = (taxable * schedule.gst_rate / schedule.money_quantum).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    ) * schedule.money_quantum
    rounded["total"] = sum(rounded.values(), Decimal(0))
    return rounded


def risk_cost_reserve(schedule, proposal, entry_side, *, as_of):
    exit_side = TransactionType.SELL if entry_side == TransactionType.BUY else TransactionType.BUY
    entry_cost = order_costs(schedule, proposal.entry * proposal.quantity, entry_side, as_of=as_of)[
        "total"
    ]
    exit_cost = max(
        order_costs(schedule, price * proposal.quantity, exit_side, as_of=as_of)["total"]
        for price in (proposal.stop, proposal.first_target, proposal.entry)
        if price is not None
    )
    return max(proposal.risk_cost_per_unit, (entry_cost + exit_cost) / proposal.quantity)
