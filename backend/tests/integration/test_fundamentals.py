"""Synthetic imports exercise revisions, receipt-time visibility, freshness and API."""

import json
from datetime import timedelta

import httpx
import pytest

from app.analysis.fundamental.source import ManualJSONSource
from app.analysis.fundamental.store import FundamentalStore
from app.core.clock import FakeClock
from app.core.enums import Exchange, InstrumentType, Segment
from app.db import session as db_session
from app.db.models.fundamental_versions import FundamentalVersion
from app.db.models.instrument import Instrument
from app.main import create_app
from tests.unit.test_fundamentals import evidence
from tests.unit.test_option_chain import OBSERVED

pytestmark = pytest.mark.usefixtures("authenticated_api")


async def seed():
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="ins-test",
                trading_symbol="TEST",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQUITY,
            )
        )


def encoded(record):
    return json.dumps([record.model_dump(mode="json")])


async def test_fundamental_point_in_time(db_engine):
    await seed()
    clock = FakeClock(OBSERVED)
    store = FundamentalStore(clock)
    original = evidence()
    identifiers = await store.import_source(
        ManualJSONSource(), encoded(original), max_age=timedelta(days=365)
    )
    assert (
        await store.import_source(
            ManualJSONSource(), encoded(original), max_age=timedelta(days=365)
        )
        == identifiers
    )
    clock.advance(timedelta(hours=1))
    changed = evidence(
        known_at=clock.now(), metrics={"pe_ratio": {"value": 30, "as_of": clock.now()}}
    )
    await store.import_source(ManualJSONSource(), encoded(changed), max_age=timedelta(days=365))
    earlier = await store.at(
        "ins-test", source="manual-test", as_of=OBSERVED, max_age=timedelta(days=365)
    )
    later = await store.at(
        "ins-test", source="manual-test", as_of=clock.now(), max_age=timedelta(days=365)
    )
    assert earlier.metrics["pe_ratio"].value == 20
    assert later.metrics["pe_ratio"].value == 30
    async with db_session.session_scope() as session:
        row = await session.get(FundamentalVersion, identifiers[0])
        assert row.payload["initial_score"]["formula"] == "QUALITY_HEALTH_V1"
        assert row.payload["initial_score"]["score"] == "1"


async def test_stale_fundamentals(db_engine, caplog):
    await seed()
    store = FundamentalStore(FakeClock(OBSERVED))
    await store.import_source(ManualJSONSource(), encoded(evidence()), max_age=timedelta(days=1))
    result = await store.at(
        "ins-test",
        source="manual-test",
        as_of=OBSERVED + timedelta(days=2),
        max_age=timedelta(days=1),
    )
    assert result.metrics["pe_ratio"] is None
    assert result.exclusions["pe_ratio"] == "STALE"
    assert "excluded as stale" in caplog.text


async def test_fundamental_receipt_and_atomic_conflicts(db_engine):
    await seed()
    store = FundamentalStore(FakeClock(OBSERVED + timedelta(minutes=1)))
    await store.import_source(ManualJSONSource(), encoded(evidence()), max_age=timedelta(days=365))
    result = await store.at(
        "ins-test", source="manual-test", as_of=OBSERVED, max_age=timedelta(days=365)
    )
    assert result.record_id is None
    with pytest.raises(ValueError, match="conflicting"):
        await store.import_source(
            ManualJSONSource(), encoded(evidence(metrics={})), max_age=timedelta(days=365)
        )
    with pytest.raises(ValueError, match="unknown"):
        await store.import_source(
            ManualJSONSource(),
            encoded(evidence(instrument_id="missing")),
            max_age=timedelta(days=365),
        )


async def test_valuation_api(db_engine, fake_clock):
    fake_clock.advance(OBSERVED - fake_clock.now())
    await seed()
    await FundamentalStore(fake_clock).import_source(
        ManualJSONSource(), encoded(evidence()), max_age=timedelta(days=365)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/fundamentals/ins-test", params={"source": "manual-test"}
        )
        assert response.status_code == 200
        assert response.json()["instrument_id"] == "ins-test"
        assert response.json()["source"] == "manual-test"
        assert response.json()["max_age_days"] == 365
        assert response.json()["as_of"] == fake_clock.now().isoformat()
        assert response.json()["valuation"]["pe_ratio"] == "20"
        assert response.json()["valuation"]["pb_ratio"] is None
        assert response.json()["valuation"]["dividend_yield"] == "0"
        missing = await client.get("/api/v1/fundamentals/missing", params={"source": "manual-test"})
        assert missing.json()["status"] == "UNAVAILABLE"
