"""SENT-004: India VIX from the existing market-data provider with explicit freshness."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.fno.chain.model import aware
from app.marketdata.base import MarketDataProvider


class VixPolicy(EvidenceModel):
    low: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    max_age_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.low >= self.high:
            raise ValueError("VIX thresholds must be ordered")
        return self


class VixReading(EvidenceModel):
    value: Decimal = Field(ge=0)
    band: Literal["LOW", "NORMAL", "HIGH"]
    observed_at: AwareDatetime
    origin: DataOrigin
    source: str


async def read_vix(
    provider: MarketDataProvider, *, as_of: datetime, policy: VixPolicy
) -> VixReading:
    aware(as_of)
    index = await provider.get_index_value("INDIA VIX")
    if index.name.upper() not in ("INDIA VIX", "INDIAVIX"):
        raise ValueError("provider returned a different index")
    if (
        not timedelta(0)
        <= as_of - aware(index.observed_at)
        <= timedelta(seconds=policy.max_age_seconds)
    ):
        raise ValueError("future or stale VIX")
    band = (
        "LOW" if index.value <= policy.low else "HIGH" if index.value >= policy.high else "NORMAL"
    )
    return VixReading(
        value=index.value,
        band=band,
        observed_at=index.observed_at,
        origin=index.data_origin,
        source=provider.name,
    )
