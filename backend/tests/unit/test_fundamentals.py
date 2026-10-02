"""Manual-source schema and financial ratios against synthetic statement references."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis.fundamental.health import balance_health
from app.analysis.fundamental.quality import growth_profitability
from app.analysis.fundamental.score import fundamental_score
from app.analysis.fundamental.source import (
    FundamentalEvidence,
    FundamentalView,
    ManualCSVSource,
    ManualJSONSource,
)
from app.analysis.fundamental.valuation import valuation_metrics
from tests.unit.test_option_chain import OBSERVED


def evidence(**changes):
    values = {
        "revenue": 120,
        "previous_revenue": 100,
        "eps": 3,
        "previous_eps": 2,
        "net_income": 12,
        "equity": 60,
        "ebit": 24,
        "capital_employed": 120,
        "operating_profit": 18,
        "debt": 30,
        "interest_expense": 6,
        "current_assets": 80,
        "current_liabilities": 40,
        "promoter_pledge": 0,
        "pe_ratio": 20,
        "market_cap": 1000,
        "dividend_yield": 0,
    }
    record = {
        "instrument_id": "ins-test",
        "source": "manual-test",
        "known_at": OBSERVED,
        "period_end": "2026-06-30",
        "metrics": {
            name: {"value": Decimal(value), "as_of": OBSERVED} for name, value in values.items()
        },
    }
    record.update(changes)
    return FundamentalEvidence.model_validate(record)


def view():
    record = evidence()
    return FundamentalView(
        record_id="fnv-test", evidence=record, metrics=record.metrics, exclusions={}
    )


def test_manual_json_roundtrip():
    record = evidence()
    content = json.dumps([record.model_dump(mode="json")])
    assert ManualJSONSource().load(content) == (record,)
    with pytest.raises(ValueError):
        ManualJSONSource().load(record.model_dump_json())
    with pytest.raises(ValidationError):
        ManualJSONSource().load('[{"instruction":"trade now"}]')


def test_manual_csv_preserves_missing_values():
    timestamp = OBSERVED.isoformat()
    csv = (
        "instrument_id,source,known_at,period_end,pe_ratio,pe_ratio_as_of,pb_ratio\n"
        f"ins-test,manual-test,{timestamp},2026-06-30,20,{timestamp},\n"
    )
    record = ManualCSVSource().load(csv)[0]
    assert record.metrics["pe_ratio"].value == 20
    assert record.metrics["pb_ratio"] is None
    with pytest.raises(ValueError):
        ManualCSVSource().load("instrument_id,source\na,b\n")
    with pytest.raises(ValidationError):
        ManualCSVSource().load(csv.replace(f"20,{timestamp},", "20,,"))


def test_valuation_metrics():
    values = valuation_metrics(view())
    assert values["pe_ratio"] == 20
    assert values["dividend_yield"] == 0
    assert values["pb_ratio"] is None


def test_growth_profitability():
    values = growth_profitability(view())
    assert values == {
        "revenue_growth": Decimal("0.2"),
        "eps_growth": Decimal("0.5"),
        "roe": Decimal("0.2"),
        "roce": Decimal("0.2"),
        "operating_margin": Decimal("0.15"),
        "net_margin": Decimal("0.1"),
    }


def test_balance_sheet_health():
    result = balance_health(view())
    assert result.metrics == {
        "debt_to_equity": Decimal("0.5"),
        "interest_coverage": Decimal(4),
        "current_ratio": Decimal(2),
        "promoter_pledge": Decimal(0),
    }
    assert result.score == 1
    assert set(result.components.values()) == {Decimal(1)}


def test_fundamental_score():
    score = fundamental_score(view())
    assert score.formula == "QUALITY_HEALTH_V1"
    assert score.score == 1
    assert score.record_id == "fnv-test"
    missing = view().model_copy(update={"metrics": {}})
    assert fundamental_score(missing).score is None
    assert balance_health(missing).score is None


@pytest.mark.parametrize(
    "changes",
    [
        {"period_end": "2027-01-01"},
        {"metrics": {"debt": {"value": -1, "as_of": OBSERVED}}},
        {"metrics": {"pe_ratio": {"value": "NaN", "as_of": OBSERVED}}},
        {"metrics": {"pe_ratio": {"value": 20, "as_of": OBSERVED + timedelta(seconds=1)}}},
        {"metrics": {"unknown": None}},
    ],
)
def test_invalid_fundamental_evidence(changes):
    with pytest.raises(ValidationError):
        evidence(**changes)
