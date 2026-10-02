"""REG-002: bounded, sourced regime observations; percent volatility and fraction breadth."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.analysis.regime.events import EventCalendar
from app.analysis.technical.ma import sma
from app.analysis.technical.trend import adx
from app.analysis.technical.volatility import historical_volatility
from app.core.data_origin import DataOrigin
from app.fno.chain.model import aware
from app.marketdata.models import Bar


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    value: Decimal
    observed_at: AwareDatetime
    available_at: AwareDatetime
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def chronology(self):
        if self.observed_at > self.available_at:
            raise ValueError("observation precedes availability incorrectly")
        return self


class IndicatorPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    adx_period: int = Field(ge=2, strict=True)
    fast_period: int = Field(ge=1, strict=True)
    slow_period: int = Field(ge=2, strict=True)
    volatility_period: int = Field(ge=2, strict=True)
    periods_per_year: int = Field(gt=0, strict=True)
    bar_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered_periods(self):
        if self.fast_period >= self.slow_period:
            raise ValueError("fast period must be below slow period")
        return self


class RegimeInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    underlying: str = Field(min_length=1)
    as_of: AwareDatetime
    data_origin: DataOrigin
    adx: Observation | None
    ma_structure: Observation | None
    realised_volatility: Observation | None
    implied_volatility: Observation | None
    breadth: Observation | None
    event_risk: Observation | None
    event_ids: tuple[str, ...]
    indicator_policy: IndicatorPolicy | None = None
    calendar: EventCalendar | None = None
    valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.valid_until is not None and self.as_of >= self.valid_until:
            raise ValueError("expired regime evidence policy")
        bounds = {
            "adx": (0, 100),
            "ma_structure": (-1, 1),
            "realised_volatility": (0, 10000),
            "implied_volatility": (0, 10000),
            "breadth": (0, 1),
            "event_risk": (0, 1),
        }
        for name, (lower, upper) in bounds.items():
            observation = getattr(self, name)
            if observation is None:
                continue
            if not lower <= observation.value <= upper:
                raise ValueError(f"out-of-range {name}")
            if observation.available_at > self.as_of:
                raise ValueError(f"future {name}")
        if self.ma_structure is not None and self.ma_structure.value not in (-1, 0, 1):
            raise ValueError("MA structure must be -1/0/1")
        if self.event_risk is not None:
            if self.event_risk.value != int(bool(self.event_ids)):
                raise ValueError("event score inconsistent with evidence")
        elif self.event_ids:
            raise ValueError("event evidence without calendar observation")
        return self


def build_inputs(
    bars: Sequence[Bar],
    *,
    underlying: str,
    as_of: datetime,
    bars_available_at: datetime,
    source: str,
    data_origin: DataOrigin,
    policy: IndicatorPolicy,
    implied_volatility: Observation | None,
    breadth: Observation | None,
    calendar: EventCalendar | None,
) -> RegimeInputs:
    """Compute from closed bars only; reject a future/invalid bar rather than hide it."""
    aware(as_of)
    aware(bars_available_at)
    if bars_available_at > as_of:
        raise ValueError("future bar availability")
    closes = []
    previous = None
    for bar in bars:
        closed_at = aware(bar.ts) + timedelta(seconds=policy.bar_seconds)
        if closed_at > bars_available_at or (previous is not None and bar.ts <= previous):
            raise ValueError("future, open or unordered bar")
        if (
            not all(value.is_finite() for value in (bar.open, bar.high, bar.low, bar.close))
            or not bar.is_valid
            or bar.low <= 0
        ):
            raise ValueError("invalid OHLC bar")
        closes.append(bar.close)
        previous = bar.ts
    lookback = max(policy.adx_period * 2 + 1, policy.slow_period, policy.volatility_period + 1)
    values = {"adx": None, "ma_structure": None, "realised_volatility": None}
    if len(bars) >= lookback:
        fast, slow = sma(closes, policy.fast_period)[-1], sma(closes, policy.slow_period)[-1]
        structure = 1 if closes[-1] > fast > slow else -1 if closes[-1] < fast < slow else 0
        numeric = {
            "adx": adx(bars, policy.adx_period).adx[-1],
            "ma_structure": Decimal(structure),
            "realised_volatility": historical_volatility(
                closes, policy.volatility_period, policy.periods_per_year
            )[-1],
        }
        for name, value in numeric.items():
            if value is not None:
                values[name] = Observation(
                    value=value,
                    observed_at=closed_at,
                    available_at=bars_available_at,
                    source=source,
                )
    active = calendar.active(underlying, as_of) if calendar is not None else None
    event = (
        None
        if active is None
        else Observation(
            value=Decimal(int(bool(active))),
            observed_at=as_of,
            available_at=as_of,
            source=calendar.source,
        )
    )
    return RegimeInputs(
        underlying=underlying,
        as_of=as_of,
        data_origin=data_origin,
        **values,
        implied_volatility=implied_volatility,
        breadth=breadth,
        event_risk=event,
        event_ids=active or (),
        indicator_policy=policy,
        calendar=calendar,
    )
