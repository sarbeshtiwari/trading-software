"""An actual child process cannot replace the parent's clock/account/gates."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.backtest.launcher import launch
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.backtest import BacktestResult, BacktestRun
from app.marketdata.recordings import write_recording
from app.monitoring.gate import get_trading_gate
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["isolated_database"]


@pytest.mark.parametrize("complete", [True, False])
async def test_actual_child_process_preserves_parent_and_persists_real_outcome(
    isolated_database, fake_clock, tmp_path, monkeypatch, complete
):
    recording, manifest = recorded_trade()
    if not complete:
        manifest = manifest.model_copy(update={"end_at": manifest.end_at - timedelta(seconds=1)})
    request_path = tmp_path / "manifest.json"
    request_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    recording_path = tmp_path / "recording.json"
    write_recording(recording_path, recording)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"database_url": str(isolated_database.url), "starting_capital": "100000"}),
        encoding="utf-8",
    )
    before = fake_clock.now()
    get_trading_gate().block("parent-review", "DO_NOT_TRADE")
    blockers = get_trading_gate().state.reasons
    monkeypatch.setenv("DATABASE_URL", "invalid-parent-database-private")
    monkeypatch.setenv("GROWW_ACCESS_TOKEN", "parent-secret-never-inherit")
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    result = await asyncio.to_thread(launch, request_path, recording_path, config_path)
    assert result.returncode == (0 if complete else 3), (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {
        "status": "COMPLETED" if complete else "INCOMPLETE",
        "run_id": manifest.run_id,
        "simulated": True,
    }
    assert "parent-secret" not in result.stdout + result.stderr
    assert get_clock() is fake_clock and fake_clock.now() == before
    assert db_session.get_engine() is isolated_database
    assert get_trading_gate().state.reasons == blockers
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, manifest.run_id)
        assert row.status == ("COMPLETED" if complete else "INCOMPLETE")
        outcome = await session.scalar(sa.select(BacktestResult))
        assert outcome.net_pnl == (Decimal("856.56") if complete else Decimal(0))
    repeated = await asyncio.to_thread(launch, request_path, recording_path, config_path)
    assert repeated.returncode == 2
    assert json.loads(repeated.stdout) == {
        "status": "FAILED",
        "error_type": "ValueError",
        "simulated": True,
    }


async def test_child_configuration_failure_does_not_echo_secret(tmp_path):
    request_path = tmp_path / "manifest.json"
    request_path.write_text('{"private": "do-not-echo-secret"}', encoding="utf-8")
    recording_path = tmp_path / "recording.json"
    recording_path.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    result = await asyncio.to_thread(launch, request_path, recording_path, config_path)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "status": "FAILED",
        "error_type": "ValidationError",
        "simulated": True,
    }
    assert "do-not-echo" not in result.stdout + result.stderr
