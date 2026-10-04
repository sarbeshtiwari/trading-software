"""Authenticated workspace exposes persisted facts, never synthetic defaults."""

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.core.enums import HealthStatus
from app.main import create_app
from app.monitoring.healthchecks import HealthReport, HealthResult, get_health_registry
from tests.integration.test_auth import credentials

__all__ = ["credentials"]


async def test_authenticated_empty_workspace(db_engine, credentials):
    contract = Path(__file__).resolve().parents[3] / "frontend" / "openapi.json"
    assert json.loads(contract.read_text(encoding="utf-8")) == create_app().openapi()
    _, password = credentials
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": password}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["trading_mode"] == "PAPER"
        assert state["account"] is None
        assert state["account_status"] == "UNAVAILABLE"
        assert state["orders"] == state["positions"] == state["chains"] == []
        assert state["broker_verification"] == "GROWW_LIVE_UNVERIFIED"
        assert not state["new_entries_allowed"]
        assert state["blockers"]
        assert any(
            row["name"] == "scheduler" and row["status"] == "DISABLED"
            for row in state["components"]
        )


@pytest.mark.parametrize("timing,expected", [
    ("fresh", "PASS"), ("boundary", "PASS"), ("stale", "STALE"),
    ("future", "STALE"), ("missing", "UNAVAILABLE"), ("naive", "UNAVAILABLE"),
    ("report_future", "STALE"), ("report_before_check", "STALE"),
])
async def test_workspace_health_observation_freshness(
    db_engine, credentials, fake_clock, monkeypatch, timing, expected
):
    now = fake_clock.utcnow()
    age = get_settings().healthcheck_interval_seconds * 2
    checked = {
        "fresh": now, "boundary": now - timedelta(seconds=age),
        "stale": now - timedelta(seconds=age + 1), "future": now + timedelta(seconds=1),
        "missing": None, "naive": now.replace(tzinfo=None),
        "report_future": now, "report_before_check": now,
    }[timing]
    report = HealthReport(results=(HealthResult(
        name="database", status=HealthStatus.PASS, critical=True,
        detail="Recorded database observation", checked_at=checked,
    ),), generated_at=(now + timedelta(seconds=1) if timing == "report_future"
                      else now - timedelta(seconds=1) if timing == "report_before_check" else now))
    monkeypatch.setattr(get_health_registry(), "_last_report", report)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        login = await client.post("/api/v1/auth/login", json={
            "username": "owner", "password": credentials[1],
        })
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        component = next(row for row in response.json()["components"] if row["name"] == "database")
        assert component["status"] == expected
        assert component["recorded_status"] == "PASS"
        assert component["critical"]
        assert (component["checked_at"] is None) == (timing in {"missing", "naive"})
        assert get_health_registry().last_report == report
