"""Authenticated workspace exposes persisted facts, never synthetic defaults."""

import json
from pathlib import Path

import httpx

from app.main import create_app
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
