"""Immutable, validated numerical limits; no agent-controlled mutation path."""

from decimal import Decimal

from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.config import Settings


class RiskLimits(EvidenceModel):
    version: int = Field(gt=0, strict=True)
    capital: Decimal = Field(gt=0)
    per_trade_risk_pct: Decimal = Field(gt=0, le=100)
    daily_loss_limit_pct: Decimal = Field(gt=0, le=100)
    max_drawdown_pct: Decimal = Field(gt=0, le=100)
    max_gross_exposure_multiple: Decimal = Field(gt=0)
    max_instrument_exposure_pct: Decimal = Field(gt=0, le=100)
    max_sector_exposure_pct: Decimal = Field(gt=0, le=100)
    max_underlying_exposure_pct: Decimal = Field(gt=0, le=100)
    max_concurrent_positions: int = Field(gt=0, strict=True)
    max_positions_per_strategy: int = Field(gt=0, strict=True)
    min_stop_distance_pct: Decimal = Field(gt=0, le=100)
    max_stop_distance_pct: Decimal = Field(gt=0, le=100)
    min_reward_risk_ratio: Decimal = Field(gt=0)
    margin_buffer_pct: Decimal = Field(ge=0, le=100)
    max_slippage_risk_pct: Decimal = Field(ge=0, le=100)
    max_market_age_seconds: int = Field(gt=0, strict=True)
    max_portfolio_age_seconds: int = Field(gt=0, strict=True)
    max_greeks_age_seconds: int = Field(gt=0, strict=True)
    max_per_trade_risk_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_loss_amount: Decimal | None = Field(default=None, gt=0)
    max_gross_exposure_amount: Decimal | None = Field(default=None, gt=0)
    max_drawdown_amount: Decimal | None = Field(default=None, gt=0)
    max_instrument_exposure_amount: Decimal | None = Field(default=None, gt=0)
    max_sector_exposure_amount: Decimal | None = Field(default=None, gt=0)
    max_underlying_exposure_amount: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_stop_distance_pct > self.max_stop_distance_pct:
            raise ValueError("invalid stop-distance bounds")
        return self

    @classmethod
    def from_settings(cls, settings: Settings, *, version: int, **extra_limits):
        if settings.starting_capital is None:
            raise ValueError("STARTING_CAPITAL_UNAVAILABLE")
        return cls(
            version=version,
            capital=settings.starting_capital,
            per_trade_risk_pct=settings.per_trade_risk_pct,
            daily_loss_limit_pct=settings.daily_loss_limit_pct,
            max_drawdown_pct=settings.max_drawdown_pct,
            max_gross_exposure_multiple=settings.max_gross_exposure_multiple,
            max_concurrent_positions=settings.max_concurrent_positions,
            min_reward_risk_ratio=settings.min_reward_risk_ratio,
            margin_buffer_pct=settings.margin_buffer_pct,
            **extra_limits,
        )


def stricter(percent_amount: Decimal, absolute_amount: Decimal | None) -> Decimal:
    return percent_amount if absolute_amount is None else min(percent_amount, absolute_amount)
