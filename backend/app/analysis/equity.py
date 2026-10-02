"""Point-in-time equity evidence shared by universe and ranking consumers."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.clock import IST
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange
from app.fno.chain.model import aware
from app.marketdata.models import Bar


class EvidenceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class DailyBar(EvidenceModel):
    closed_at: AwareDatetime
    available_at: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0, strict=True)
    traded_value: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_bar(self):
        if self.available_at < self.closed_at:
            raise ValueError("bar not closed at availability")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC")
        return self

    def technical_bar(self) -> Bar:
        return Bar(self.closed_at, self.open, self.high, self.low, self.close, self.volume)


class PriceObservation(EvidenceModel):
    value: Decimal = Field(gt=0)
    observed_at: AwareDatetime
    available_at: AwareDatetime

    @model_validator(mode="after")
    def chronology(self):
        if self.available_at < self.observed_at:
            raise ValueError("price not observed at availability")
        return self


class EquitySnapshot(EvidenceModel):
    symbol: str = Field(min_length=1)
    exchange: Exchange
    effective_at: AwareDatetime
    known_at: AwareDatetime
    source: str = Field(min_length=1)
    data_origin: DataOrigin
    active: bool
    restricted: bool
    fno_eligible: bool
    indices: tuple[str, ...]
    sector: str | None = Field(default=None, min_length=1)
    industry: str | None = Field(default=None, min_length=1)
    price: PriceObservation
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    daily: tuple[DailyBar, ...]
    preopen: PriceObservation | None = None
    official_open: PriceObservation | None = None

    @model_validator(mode="after")
    def chronology(self):
        if self.effective_at > self.known_at or self.price.available_at > self.known_at:
            raise ValueError("snapshot contains future knowledge")
        dates = [bar.closed_at.astimezone(IST).date() for bar in self.daily]
        if dates != sorted(set(dates)):
            raise ValueError("daily bars must have unique increasing session dates")
        if any(bar.available_at > self.known_at for bar in self.daily):
            raise ValueError("snapshot contains future bar knowledge")
        for price in (self.preopen, self.official_open):
            if price is not None and price.available_at > self.known_at:
                raise ValueError("snapshot contains future price knowledge")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("crossed equity book")
        return self

    @property
    def key(self) -> str:
        return f"{self.exchange.value}:{self.symbol}"

    def require_as_of(self, as_of: datetime, max_age: timedelta) -> None:
        aware(as_of)
        if self.known_at > as_of or self.effective_at > as_of:
            raise ValueError("future equity evidence")
        if max_age < timedelta(0) or as_of - self.price.observed_at > max_age:
            raise ValueError("stale equity price")


def aligned_history(
    member: EquitySnapshot,
    benchmark: EquitySnapshot,
    *,
    as_of: datetime,
    lookback: int,
    max_age: timedelta,
) -> tuple[tuple[DailyBar, ...], tuple[DailyBar, ...]]:
    """Require identical session dates; never bridge missing days silently."""
    aware(as_of)
    if type(lookback) is not int or lookback < 1 or max_age < timedelta(0):
        raise ValueError("invalid historical window")
    if member.known_at > as_of or benchmark.known_at > as_of:
        raise ValueError("future history knowledge")
    if member.data_origin != benchmark.data_origin:
        raise ValueError("mixed history provenance")
    series = (member.daily[-lookback - 1 :], benchmark.daily[-lookback - 1 :])
    if any(len(bars) != lookback + 1 for bars in series):
        raise ValueError("insufficient daily history")
    if any(as_of - bars[-1].closed_at > max_age for bars in series):
        raise ValueError("stale daily history")
    if [bar.closed_at for bar in series[0]] != [bar.closed_at for bar in series[1]]:
        raise ValueError("unaligned benchmark sessions")
    return series
