"""Authenticated reads expose the real historical worker's persisted results."""

import hashlib
from decimal import Decimal

import httpx
import sqlalchemy as sa

from app.api import backtests
from app.backtest.bootstrap import prepare_run
from app.backtest.engine import run_prepared
from app.config import get_settings
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestRun
from app.main import create_app
from tests.integration.test_auth import credentials
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["credentials", "isolated_database"]


async def login(client, password):
    response = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"


async def test_actual_run_results_curves_trades_and_authentication(
    isolated_database, credentials, fake_clock, tmp_path, monkeypatch
):
    recording, request = recorded_trade()
    fake_clock.set_to(request.start_at)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "api.lock",
    )
    assert await run_prepared(worker, request) == "COMPLETED"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        assert (await client.get("/api/v1/backtests")).status_code == 401
        assert (await client.get(f"/api/v1/backtests/{request.run_id}/export")).status_code == 401
        await login(client, credentials[1])
        workspace = await client.get("/api/v1/workspace")
        assert workspace.status_code == 200, workspace.text
        listed = await client.get("/api/v1/backtests")
        assert listed.status_code == 200
        assert listed.json()["simulated"] is True
        assert listed.json()["runs"][0]["id"] == request.run_id
        detail = (await client.get(f"/api/v1/backtests/{request.run_id}")).json()
        assert detail["run"]["status"] == "COMPLETED"
        assert detail["catalog_integrity"] == "AUDIT_BOUND"
        assert detail["simulated"] is True
        disclosure = detail["universe_disclosure"]
        assert disclosure["method"] == "OWNER_DECLARED_STATIC_LISTS"
        assert disclosure["historical_eligibility_verified"] is False
        assert disclosure["completeness_verified"] is False
        assert "Survivorship and selection bias remain possible" in disclosure["limitation"]
        assert len(disclosure["windows"]) == 1
        declared = disclosure["windows"][0]
        assert declared["source_run_id"] == request.run_id
        assert [item["id"] for item in declared["instruments"]] == [
            item.id for item in request.instruments
        ]
        assert [
            item["id"] for item in declared["instruments"] if item["role"] == "STRATEGY_INSTRUMENT"
        ] == [request.strategy_instrument_id]
        assert any(item["role"] == "CONTEXT_ONLY" for item in declared["instruments"])
        assert detail["run"]["simulated"] and not detail["launch_available"]
        assert detail["results"][0]["net_pnl"] == "856.56"
        assert detail["results"][0]["simulated"] is True
        assert detail["results"][0]["sharpe"] is None
        assert detail["recording_sha256"] == recording.content_hash()
        assert detail["reproducibility"]["simulated"]
        assert detail["reproducibility"]["version"] == 1
        assert detail["strategy_binding"]["specification"]["id"] == "closed-candle-breakout"
        assert (
            detail["strategy_binding"]["specification"]["risk"]["requested_risk_fraction"]
            == "0.005"
        )
        assert len(detail["reproducibility"]["outcomes_sha256"]) == 64
        assert "parameters" not in detail["run"] and "database_url" not in str(detail)
        trades = (await client.get(f"/api/v1/backtests/{request.run_id}/trades")).json()
        assert trades["trades"][0]["quantity"] == 111
        assert trades["trades"][0]["simulated"] is True
        assert trades["simulated"] is True
        first = (await client.get(f"/api/v1/backtests/{request.run_id}/samples?limit=2")).json()
        assert first["has_more"] and len(first["samples"]) == 2
        assert first["simulated"] is True
        assert first["samples"][1]["drawdown"] == "0.0001384"
        assert first["samples"][1]["simulated"] is True
        last = (await client.get(f"/api/v1/backtests/{request.run_id}/samples?offset=2")).json()
        assert not last["has_more"] and last["samples"][0]["net_equity"] == "100856.56"
        assert (await client.get("/api/v1/backtests?limit=0")).status_code == 422
        assert (await client.get("/api/v1/backtests/missing")).status_code == 404
        assert (await client.post("/api/v1/backtests", json={})).status_code == 405
        export_path = f"/api/v1/backtests/{request.run_id}/export"
        exported = await client.get(export_path)
        assert exported.status_code == 200, exported.text
        report = exported.json()
        assert report["simulated"] is True and report["catalog_integrity"] == "AUDIT_BOUND"
        assert report["universe_disclosure"] == disclosure
        assert report["trades"][0]["net_pnl"] == "856.56"
        assert report["results"][0]["equity_curve"][-1][1] == "100856.56"
        assert "parameters" not in report["run"] and "database_url" not in exported.text
        assert exported.headers["cache-control"] == "no-store"
        assert "simulated-backtest.json" in exported.headers["content-disposition"]
        async with db_session.session_scope() as session:
            event = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "HISTORICAL_REPORT_EXPORTED")
            )
            assert event.actor == "owner"
            assert event.result["export_sha256"] == hashlib.sha256(exported.content).hexdigest()
        with monkeypatch.context() as patch:
            patch.setattr(backtests, "MAX_EXPORT_ROWS", 0)
            assert (await client.get(export_path)).status_code == 413
        with monkeypatch.context() as patch:
            patch.setattr(backtests, "MAX_EXPORT_BYTES", 1)
            assert (await client.get(export_path)).status_code == 413
        async with db_session.session_scope() as session:
            await session.execute(
                sa.update(BacktestRun)
                .where(BacktestRun.id == request.run_id)
                .values(survivorship_note="Falsely certified universe")
            )
        for suffix in ("", "/trades", "/samples", "/export"):
            refused = await client.get(f"/api/v1/backtests/{request.run_id}{suffix}")
            assert refused.status_code == 409
        async with db_session.session_scope() as session:
            await session.execute(
                sa.update(BacktestRun)
                .where(BacktestRun.id == request.run_id)
                .values(simulated=False)
            )
        assert (await client.get("/api/v1/backtests")).status_code == 409
        assert (await client.get(export_path)).status_code == 409


async def test_legacy_universe_is_unavailable_not_backfilled_or_certified(
    isolated_database, credentials, fake_clock, tmp_path
):
    recording, request = recorded_trade()
    fake_clock.set_to(request.start_at)
    worker = await prepare_run(
        request,
        recording,
        settings=get_settings().model_copy(update={"starting_capital": Decimal(100000)}),
        clock=fake_clock,
        lock_path=tmp_path / "legacy.lock",
    )
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        row.assumptions = {
            key: value
            for key, value in row.assumptions.items()
            if key not in {"universe_disclosure", "window_disclosure"}
        }
        row.survivorship_note = None
        row.data_window_warning = None
    assert await run_prepared(worker, request) == "COMPLETED"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        await login(client, credentials[1])
        for suffix in ("", "/export"):
            response = await client.get(f"/api/v1/backtests/{request.run_id}{suffix}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["catalog_integrity"] == "AUDIT_BOUND"
            assert body["universe_disclosure"] is None
            assert body["window_disclosure"] is None
            assert "UNAVAILABLE" in body["universe_warning"]
            assert "Survivorship and selection bias remain possible" in body["universe_warning"]
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, request.run_id)
        assert "universe_disclosure" not in row.assumptions
        assert "window_disclosure" not in row.assumptions
        assert row.data_window_warning is None
        assert row.survivorship_note is None


async def test_empty_current_database_does_not_fabricate_historical_results(db_engine, credentials):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        await login(client, credentials[1])
        response = await client.get("/api/v1/backtests")
        assert response.status_code == 200
        assert response.json()["runs"] == [] and not response.json()["has_more"]
        assert "not automatically published" in response.json()["scope"]
