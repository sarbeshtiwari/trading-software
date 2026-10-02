"""Validated sizing evidence and owner-configured policy."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.config import Settings
from app.core.data_origin import DataOrigin
from app.core.enums import SignalDirection


class SizingPolicy(EvidenceModel):
    configured_capital: Decimal | None = Field(default=None, gt=0)
    per_trade_risk_pct: Decimal = Field(gt=0, le=100)
    daily_loss_limit_pct: Decimal = Field(gt=0, le=100)
    max_gross_exposure_multiple: Decimal = Field(gt=0)
    max_instrument_exposure_pct: Decimal = Field(gt=0, le=100)
    margin_buffer_pct: Decimal = Field(ge=0, le=100)
    max_age_seconds: int = Field(gt=0, strict=True)
    kelly_enabled: bool = False
    kelly_fraction_cap: Decimal | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def kelly_cap_required(self):
        if self.kelly_enabled and self.kelly_fraction_cap is None:
            raise ValueError("Kelly requires an explicitly configured fraction cap")
        return self

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        max_instrument_exposure_pct: Decimal,
        max_age_seconds: int,
        **options,
    ):
        return cls(
            configured_capital=settings.starting_capital,
            per_trade_risk_pct=settings.per_trade_risk_pct,
            daily_loss_limit_pct=settings.daily_loss_limit_pct,
            max_gross_exposure_multiple=settings.max_gross_exposure_multiple,
            margin_buffer_pct=settings.margin_buffer_pct,
            max_instrument_exposure_pct=max_instrument_exposure_pct,
            max_age_seconds=max_age_seconds,
            **options,
        )


class SizingInputs(EvidenceModel):
    proposal_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    as_of: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    source_ids: tuple[str, ...]
    data_origin: DataOrigin
    equity: Decimal = Field(ge=0)
    available_margin: Decimal = Field(ge=0)
    gross_exposure: Decimal = Field(ge=0)
    instrument_exposure: Decimal = Field(ge=0)
    daily_loss: Decimal = Field(ge=0)
    reserved_risk: Decimal = Field(ge=0)
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]
    lot_size: int = Field(gt=0, strict=True)
    tick_size: Decimal = Field(gt=0)
    margin_per_unit: Decimal = Field(gt=0)
    exposure_per_unit: Decimal = Field(gt=0)
    risk_cost_per_unit: Decimal = Field(ge=0)
    defined_max_loss_per_unit: Decimal | None = Field(default=None, gt=0)
    requested_risk_pct: Decimal = Field(gt=0, le=100)
    atr: Decimal | None = Field(default=None, gt=0)
    reference_atr: Decimal | None = Field(default=None, gt=0)
    win_probability: Decimal | None = Field(default=None, ge=0, le=1)
    payoff_ratio: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def coherent(self):
        if not self.source_ids or any(not source.strip() for source in self.source_ids):
            raise ValueError("sizing evidence sources required")
        if self.instrument_exposure > self.gross_exposure:
            raise ValueError("instrument exposure exceeds gross exposure")
        if (self.atr is None) != (self.reference_atr is None):
            raise ValueError("ATR adjustment requires both observed and reference ATR")
        sign = 1 if self.direction == SignalDirection.LONG else -1
        if sign * (self.entry - self.stop) <= 0:
            raise ValueError("stop must be protective")
        return self


class SizingResult(EvidenceModel):
    formula_version: Literal["1.0.0", "1.1.0"] = "1.0.0"
    quantity: int = Field(ge=0, strict=True)
    capital: Decimal = Field(ge=0)
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    risk_budget: Decimal = Field(ge=0)
    risk_per_unit: Decimal = Field(gt=0)
    raw_quantity: Decimal = Field(ge=0)
    allocated_risk: Decimal = Field(ge=0)
    binding_constraint: str
    zero_reason: str | None
    caps: dict[str, Decimal]
