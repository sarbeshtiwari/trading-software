"""FUND-001/008: strict manual CSV/JSON sources and point-in-time metric evidence."""

import csv
import io
import json
from datetime import date
from decimal import Decimal
from typing import Protocol

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.clock import IST

METRICS = frozenset(
    {
        "market_cap",
        "pe_ratio",
        "pb_ratio",
        "ev_ebitda",
        "dividend_yield",
        "book_value",
        "revenue",
        "previous_revenue",
        "eps",
        "previous_eps",
        "net_income",
        "equity",
        "ebit",
        "capital_employed",
        "operating_profit",
        "debt",
        "interest_expense",
        "current_assets",
        "current_liabilities",
        "promoter_pledge",
    }
)


class Metric(EvidenceModel):
    value: Decimal
    as_of: AwareDatetime


class FundamentalEvidence(EvidenceModel):
    instrument_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    period_end: date
    metrics: dict[str, Metric | None]

    @model_validator(mode="after")
    def evidence_dates(self):
        if set(self.metrics) - METRICS:
            raise ValueError("unsupported fundamental metric")
        if self.period_end > self.known_at.astimezone(IST).date():
            raise ValueError("future reporting period")
        if any(
            metric is not None and metric.as_of > self.known_at for metric in self.metrics.values()
        ):
            raise ValueError("future metric knowledge")
        pledge = self.metrics.get("promoter_pledge")
        if pledge is not None and not 0 <= pledge.value <= 1:
            raise ValueError("promoter pledge must be a fraction")
        for name in (
            "debt",
            "interest_expense",
            "current_assets",
            "current_liabilities",
            "market_cap",
        ):
            metric = self.metrics.get(name)
            if metric is not None and metric.value < 0:
                raise ValueError(f"negative {name}")
        return self


class FundamentalSource(Protocol):
    def load(self, content: str) -> tuple[FundamentalEvidence, ...]: ...


class FundamentalView(EvidenceModel):
    record_id: str | None
    evidence: FundamentalEvidence | None
    metrics: dict[str, Metric | None]
    exclusions: dict[str, str]


class ManualJSONSource:
    def load(self, content: str) -> tuple[FundamentalEvidence, ...]:
        rows = json.loads(content)
        if not isinstance(rows, list):
            raise ValueError("JSON fundamentals must be an array")
        return tuple(FundamentalEvidence.model_validate(row) for row in rows)


class ManualCSVSource:
    def load(self, content: str) -> tuple[FundamentalEvidence, ...]:
        reader = csv.DictReader(io.StringIO(content))
        metadata = {"instrument_id", "source", "known_at", "period_end"}
        allowed = metadata | METRICS | {f"{name}_as_of" for name in METRICS}
        headers = reader.fieldnames or []
        if (
            len(headers) != len(set(headers))
            or not metadata.issubset(headers)
            or set(headers) - allowed
        ):
            raise ValueError("invalid fundamental CSV headers")
        result = []
        for row in reader:
            if None in row:
                raise ValueError("malformed CSV row")
            metrics = {}
            for name in METRICS.intersection(headers):
                value = row.get(name)
                metrics[name] = (
                    None
                    if value in (None, "")
                    else {"value": value, "as_of": row.get(f"{name}_as_of")}
                )
            result.append(
                FundamentalEvidence.model_validate(
                    {**{name: row[name] for name in metadata}, "metrics": metrics}
                )
            )
        return tuple(result)
