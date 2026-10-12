"""Explicit state snapshots consumed by the pure risk engine."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.core.enums import SignalDirection
from app.modes import TradingMode
from app.risk.entry_models import EntryEvidence


class EvidenceTime(EvidenceModel):
    observed_at: AwareDatetime
    available_at: AwareDatetime
    source_id: str = Field(min_length=1)
    data_origin: DataOrigin


class RiskProposal(EvidenceModel):
    id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    generated_at: AwareDatetime
    data_origin: DataOrigin
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]
    quantity: int = Field(gt=0, strict=True)
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0)
    entry: Decimal = Field(gt=0)
    stop: Decimal | None = Field(default=None, gt=0)
    first_target: Decimal | None = Field(default=None, gt=0)
    exposure_per_unit: Decimal = Field(gt=0)
    margin_per_unit: Decimal = Field(gt=0)
    risk_cost_per_unit: Decimal = Field(ge=0)
    is_option: bool
    defined_max_loss_per_unit: Decimal | None = Field(default=None, gt=0)

    @property
    def planned_risk_per_unit(self) -> Decimal:
        if self.stop is None:
            return self.entry + self.risk_cost_per_unit
        return (
            max(abs(self.entry - self.stop), self.defined_max_loss_per_unit or Decimal(0))
            + self.risk_cost_per_unit
        )


class Exposure(EvidenceModel):
    instrument_id: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    #: Always the absolute size of the exposure; the sign lives in ``direction``
    #: so gross and net can never be read off the same number by accident.
    notional: Decimal = Field(ge=0)
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]

    @property
    def signed_notional(self) -> Decimal:
        """Notional signed by the side actually traded.

        This is a sum over trade sides, *not* a delta-adjusted figure: a long put
        contributes positive notional here even though it is directionally short
        the underlying. Delta-weighted directional exposure is a Greeks concern
        (GRK-*), and no risk limit consumes this measure.
        """
        return self.notional if self.direction == SignalDirection.LONG else -self.notional


class PortfolioState(EvidenceTime):
    equity: Decimal = Field(ge=0)
    peak_equity: Decimal = Field(gt=0)
    realised_day_pnl: Decimal
    unrealised_day_pnl: Decimal
    available_margin: Decimal = Field(ge=0)
    #: Margin already committed, including unfilled entry commitments — the same
    #: commitments `available_margin` has already been reduced by, so the two sum
    #: to total margin capital and utilisation cannot be double-counted.
    used_margin: Decimal = Field(ge=0)
    reserved_risk: Decimal = Field(ge=0)
    exposures: tuple[Exposure, ...]
    open_and_pending_positions: int = Field(ge=0, strict=True)
    strategy_open_and_pending_positions: int = Field(ge=0, strict=True)
    strategy_id: str = Field(min_length=1)
    entries_blocked: bool
    drawdown_disarmed: bool

    @model_validator(mode="after")
    def coherent(self):
        if self.equity > self.peak_equity:
            raise ValueError("peak equity cannot be below equity")
        if self.strategy_open_and_pending_positions > self.open_and_pending_positions:
            raise ValueError("strategy count exceeds global count")
        return self

    # PORT-004 defines these three measures, and every consumer reads them from
    # here rather than re-summing `exposures`: a limit evaluated against a
    # measure some other caller computed differently is the whole hazard.

    @property
    def margin_capacity(self) -> Decimal:
        """Total margin capital: what is committed plus what is still free."""
        return self.used_margin + self.available_margin

    @property
    def gross_exposure(self) -> Decimal:
        """Total exposure carried, long and short alike. Limits use this."""
        return sum((item.notional for item in self.exposures), Decimal(0))

    @property
    def net_exposure(self) -> Decimal:
        """Longs less shorts. Never a substitute for gross, and never capacity."""
        return sum((item.signed_notional for item in self.exposures), Decimal(0))

    def exposure_for(self, attribute: str, key: str) -> Decimal:
        """Gross exposure on one concentration dimension."""
        return sum(
            (item.notional for item in self.exposures if getattr(item, attribute) == key),
            Decimal(0),
        )


class BookLevel(EvidenceModel):
    price: Decimal = Field(gt=0)
    quantity: int = Field(gt=0, strict=True)


class MarketState(EvidenceTime):
    as_of: AwareDatetime
    instrument_id: str = Field(min_length=1)
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    greeks: EvidenceTime | None = None
    ban_listed: bool
    news_halt: bool
    event_blackout: bool
    manually_blocked: bool
    mode: TradingMode

    @model_validator(mode="after")
    def ordered(self):
        if tuple(sorted(self.asks, key=lambda level: level.price)) != self.asks:
            raise ValueError("asks must be sorted ascending")
        if tuple(sorted(self.bids, key=lambda level: level.price, reverse=True)) != self.bids:
            raise ValueError("bids must be sorted descending")
        if self.bids and self.asks and self.bids[0].price > self.asks[0].price:
            raise ValueError("crossed depth book")
        return self


class RuleResult(EvidenceModel):
    rule: str
    passed: bool
    rejection_code: str
    inputs: dict[str, str | int | bool | None]


class Decision(EvidenceModel):
    entry_policy: EntryEvidence | None = None
    formula_version: Literal["1.0.0", "1.1.0"] = "1.0.0"
    approved: bool
    binding_rule: str | None
    rejection_code: str | None
    approved_quantity: int = Field(ge=0)
    risk_amount: Decimal = Field(ge=0)
    rules: tuple[RuleResult, ...]
