"""Duration disclosure distinguishes sparse recordings from statistical adequacy."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.backtest.adequacy import disclose_window
from app.backtest.bootstrap import prepare_run
from app.backtest.engine import run_prepared
from app.config import Settings, get_settings
from app.db import session as db_session
from app.db.models.backtest import BacktestRun
from tests.integration.test_historical_bootstrap import isolated_database
from tests.integration.test_historical_engine import recorded_trade

__all__ = ["isolated_database"]


@pytest.mark.parametrize(
    "seconds,state,count", [(4, "BELOW_MINIMUM", 2), (86400, "SPAN_MEETS_MINIMUM", 3)]
)
def test_sparse_window_never_claims_confidence(seconds, state, count):
    recording, manifest = recorded_trade()
    manifest = manifest.model_copy(
        update={"end_at": manifest.start_at + timedelta(seconds=seconds)}
    )
    window = disclose_window(manifest, recording, 1)["windows"][0]
    assert window["elapsed_seconds"] == str(seconds)
    assert window["span_state"] == state
    assert window["recorded_publications"] == count
    assert window["continuous_coverage_verified"] is False
    assert window["statistical_confidence_verified"] is False
    assert window["first_publication_at"] == manifest.start_at.isoformat().replace("+00:00", "Z")


def test_minimum_days_must_be_positive():
    with pytest.raises(ValueError):
        Settings(_env_file=None, min_backtest_days=0)


def test_elapsed_duration_uses_instants_not_dst_wall_clock():
    recording, manifest = recorded_trade()
    manifest = manifest.model_copy(
        update={
            "start_at": datetime(2026, 3, 8, tzinfo=ZoneInfo("America/New_York")),
            "end_at": datetime(2026, 3, 9, tzinfo=ZoneInfo("America/New_York")),
        }
    )
    window = disclose_window(manifest, recording, 1)["windows"][0]
    assert window["elapsed_seconds"] == "82800"
    assert window["span_state"] == "BELOW_MINIMUM"
    assert window["recorded_publications"] == 0
    assert window["first_publication_at"] is None and window["last_publication_at"] is None


async def test_duration_policy_is_captured_and_sealed(isolated_database, fake_clock, tmp_path):
    recording, manifest = recorded_trade()
    fake_clock.set_to(manifest.start_at)
    settings = get_settings().model_copy(
        update={"starting_capital": Decimal(100000), "min_backtest_days": 2}
    )
    worker = await prepare_run(
        manifest,
        recording,
        settings=settings,
        clock=fake_clock,
        lock_path=tmp_path / "duration.lock",
    )
    settings.min_backtest_days = 3
    with pytest.raises(ValueError, match="configuration changed"):
        await run_prepared(worker, manifest)
    settings.min_backtest_days = 2
    assert await run_prepared(worker, manifest) == "COMPLETED"
    async with db_session.session_scope() as session:
        row = await session.get(BacktestRun, manifest.run_id)
        window = row.assumptions["window_disclosure"]["windows"][0]
        assert window["minimum_calendar_days"] == 2
        assert window["elapsed_seconds"] == "5"
        assert "BELOW_MINIMUM" in row.data_window_warning
        assert "configured minimum 2 calendar days" in row.data_window_warning
