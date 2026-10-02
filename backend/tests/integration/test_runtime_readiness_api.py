from datetime import timedelta

import httpx

from app.instruments.loader import InstrumentLoader
from app.main import create_app
from app.monitoring.healthchecks import HealthReport, get_health_registry


async def test_readiness_requires_authentication(db_engine):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/system/readiness")).status_code == 401


async def test_empty_runtime_does_not_claim_trading_readiness(db_engine, authenticated_api):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/system/readiness")
    assert response.status_code == 200
    data = response.json()
    assert not data["entry_gate_open"]
    assert not data["execution_worker_enabled"]
    assert data["health_state"] == "UNAVAILABLE"
    assert data["instrument_count"] == 0
    assert data["instrument_snapshot_audit_verified"] is None
    assert data["calendar_warning"]
    assert data["groww_live_execution"] == "UNVERIFIED"


async def test_readiness_exposes_real_catalog_audit_and_stale_health(
    db_engine, authenticated_api, fake_clock
):
    result = await InstrumentLoader().load(
        "exchange,segment,trading_symbol,instrument_type,lot_size,tick_size\n"
        "NSE,CASH,ISOLATED_TEST,EQ,1,0.01\n"
    )
    get_health_registry()._last_report = HealthReport(
        results=(), generated_at=fake_clock.utcnow() - timedelta(hours=1)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        data = (await client.get("/api/v1/system/readiness")).json()
    assert data["health_state"] == "STALE"
    assert data["instrument_count"] == 1
    assert data["restricted_instrument_count"] == 1
    assert data["instrument_snapshot_id"] == result.snapshot_id
    assert data["instrument_snapshot_source"] == "SUPPLIED_CSV"
    assert data["instrument_snapshot_audit_verified"] is True
