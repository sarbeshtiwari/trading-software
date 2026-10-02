"""STRAT-003: advisory signals with no executable quantity or risk-control fields."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.core.enums import Product, SignalDirection


class Signal(EvidenceModel):
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    instrument_key: str = Field(min_length=1)
    generated_at: AwareDatetime
    data_origin: DataOrigin
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]
    product: Product
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    targets: tuple[Decimal, ...]
    timeframe_seconds: int = Field(gt=0, strict=True)
    confidence: Decimal = Field(ge=0, le=1)
    conditions_fired: tuple[str, ...]

    @model_validator(mode="after")
    def protective_prices(self):
        sign = 1 if self.direction == SignalDirection.LONG else -1
        if sign * (self.entry - self.stop) <= 0:
            raise ValueError("stop must be strictly on the protective side")
        if not self.targets or any(
            target <= 0 or sign * (target - self.entry) <= 0 for target in self.targets
        ):
            raise ValueError("target must be on the profit side")
        if not self.conditions_fired or any(
            not condition.strip() for condition in self.conditions_fired
        ):
            raise ValueError("trigger evidence required")
        return self
