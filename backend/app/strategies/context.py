"""OC-008: schema-validated numerical context for strategies and future AI consumers.

Broker raw text is deliberately excluded. Builders enforce decision-time freshness;
consumers must rebuild context for each decision rather than reuse a past decision.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.data_origin import DataOrigin
from app.fno.chain.buildup import BuildUp, buildup
from app.fno.chain.levels import OILevels, oi_levels
from app.fno.chain.maxpain import max_pain
from app.fno.chain.model import aware, require_as_of
from app.fno.chain.pcr import pcr
from app.fno.chain.skew import IVPoint, iv_skew
from app.marketdata.models import Bar, OptionChain


class ContextValue(BaseModel):
    """Timestamp/provenance envelope; raw external text cannot set control fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    observed_at: AwareDatetime
    available_at: AwareDatetime
    origin: DataOrigin
    value: object


def validate_context_inputs(required, available, *, as_of, max_age, origin, timeframe_seconds):
    for name in required:
        item = available.get(name)
        if not isinstance(item, ContextValue) or item.origin != origin:
            raise ValueError("missing or unprovenanced strategy input")
        if not item.observed_at <= item.available_at <= as_of or as_of - item.observed_at > max_age:
            raise ValueError("future or stale strategy input")
        if item.value is None:
            raise ValueError("required strategy input unavailable")
        if name == "candles":
            if not isinstance(item.value, (list, tuple)) or not item.value:
                raise ValueError("missing strategy candles")
            previous = None
            for bar in item.value:
                if (
                    not isinstance(bar, Bar)
                    or bar.ts.utcoffset() is None
                    or any(
                        not price.is_finite() or price <= 0
                        for price in (bar.open, bar.high, bar.low, bar.close)
                    )
                    or not bar.is_valid
                    or bar.ts + timedelta(seconds=timeframe_seconds) > item.available_at
                    or (previous is not None and bar.ts <= previous)
                ):
                    raise ValueError("invalid or future strategy candle")
                previous = bar.ts
            if as_of - (previous + timedelta(seconds=timeframe_seconds)) > max_age:
                raise ValueError("stale strategy candles")


class StrategyContext:
    """STRAT-004/FUND-007: expose only explicitly declared context attributes."""

    __slots__ = ("_values", "as_of")

    def __init__(self, values: dict, *, as_of: datetime | None = None) -> None:
        if as_of is not None:
            aware(as_of)
        object.__setattr__(self, "_values", MappingProxyType(deepcopy(values)))
        object.__setattr__(self, "as_of", as_of)

    def __getattr__(self, name: str):
        if name not in self._values:
            raise AttributeError(f"undeclared strategy input: {name}")
        return self._values[name]

    def __setattr__(self, name, value):
        raise AttributeError("strategy context is read-only")

    def __delattr__(self, name):
        raise AttributeError("strategy context is read-only")


def build_strategy_context(
    required_inputs: tuple[str, ...], available: dict, *, as_of: datetime | None = None
) -> StrategyContext:
    allowed = {
        "candles",
        "indicators",
        "regime",
        "chain",
        "news",
        "sentiment",
        "fundamentals",
        "equity",
    }
    if not set(required_inputs).issubset(allowed):
        raise ValueError("unsupported strategy input")
    missing = set(required_inputs) - available.keys()
    if missing or any(available[name] is None for name in required_inputs):
        raise ValueError("required strategy input unavailable")
    return StrategyContext(
        {
            name: available[name].value
            if isinstance(available[name], ContextValue)
            else available[name]
            for name in required_inputs
        },
        as_of=as_of,
    )


class StrikeBuildUp(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    strike: Decimal = Field(gt=0)
    call: BuildUp
    put: BuildUp


class ChainContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    underlying: str = Field(min_length=1)
    expiry: date
    observed_at: AwareDatetime
    decision_at: AwareDatetime
    data_origin: DataOrigin
    spot: Decimal | None = Field(default=None, gt=0)
    max_pain: Decimal | None = Field(default=None, gt=0)
    pcr_oi: Decimal | None = Field(default=None, ge=0)
    pcr_volume: Decimal | None = Field(default=None, ge=0)
    levels: OILevels
    iv_unit: Literal["PERCENTAGE_POINTS"] = "PERCENTAGE_POINTS"
    skew: tuple[IVPoint, ...]
    buildup: tuple[StrikeBuildUp, ...]
    missing_value: Literal["UNAVAILABLE"] = "UNAVAILABLE"


def build_chain_context(
    chain: OptionChain,
    *,
    as_of: datetime,
    max_age: timedelta,
    previous: OptionChain | None = None,
    max_comparison_gap: timedelta | None = None,
) -> ChainContext:
    require_as_of(chain, as_of, max_age)
    matrix = {}
    if previous is not None:
        if max_comparison_gap is None:
            raise ValueError("comparison freshness policy required")
        matrix = buildup(chain, previous, max_gap=max_comparison_gap)
    return ChainContext(
        underlying=chain.underlying,
        expiry=chain.expiry,
        observed_at=chain.observed_at,
        decision_at=as_of,
        data_origin=chain.data_origin,
        spot=chain.spot,
        max_pain=max_pain(chain),
        pcr_oi=pcr(chain),
        pcr_volume=pcr(chain, "volume"),
        levels=oi_levels(chain),
        skew=iv_skew(chain),
        buildup=tuple(
            StrikeBuildUp(strike=strike, **sides) for strike, sides in sorted(matrix.items())
        ),
    )
