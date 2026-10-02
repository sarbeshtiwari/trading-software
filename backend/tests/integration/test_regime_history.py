"""Synthetic persisted hysteresis history and receipt-time no-lookahead tests."""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from alembic import command
from app.analysis.regime.history import RegimeStore
from app.config import get_settings
from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from app.core.enums import MarketRegime
from app.db.models import metadata
from tests.conftest import BACKEND_ROOT
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_regime import inputs, policy


async def test_regime_history_attribution(db_engine):
    clock = FakeClock(OBSERVED)
    store = RegimeStore(clock)
    record_id, first = await store.record(inputs(adx=20), policy())
    assert await store.record(inputs(adx=20), policy()) == (record_id, first)
    clock.advance(timedelta(seconds=1))
    await store.record(inputs(clock.now()), policy())
    restarted = RegimeStore(clock)
    clock.advance(timedelta(seconds=1))
    latest_id, latest = await restarted.record(inputs(clock.now()), policy())
    assert latest.label == MarketRegime.TRENDING_UP
    assert latest.changes == 1
    assert latest_id != record_id
    assert await restarted.at("TEST", DataOrigin.SYNTHETIC, OBSERVED) == (record_id, first)
    assert await restarted.at("TEST", DataOrigin.LIVE, clock.now()) is None
    with pytest.raises(ValueError, match="conflicting"):
        await restarted.record(inputs(clock.now(), adx=10), policy())


async def test_regime_history_late_receipt_and_future(db_engine):
    clock = FakeClock(OBSERVED + timedelta(minutes=5))
    store = RegimeStore(clock)
    await store.record(inputs(), policy())
    assert await store.at("TEST", DataOrigin.SYNTHETIC, OBSERVED) is None
    assert await store.at("TEST", DataOrigin.SYNTHETIC, clock.now()) is not None
    with pytest.raises(ValueError, match="future"):
        await store.record(inputs(clock.now() + timedelta(seconds=1)), policy())


async def test_regime_history_rejects_out_of_order(db_engine):
    clock = FakeClock(OBSERVED + timedelta(minutes=1))
    store = RegimeStore(clock)
    await store.record(inputs(clock.now()), policy())
    with pytest.raises(ValueError, match="advance"):
        await store.record(inputs(), policy())


def test_regime_migration_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert "regime_history" in sa.inspect(connection).get_table_names()
            differences = compare_metadata(MigrationContext.configure(connection), metadata)
            assert not differences
        command.downgrade(config, "0002_paper_state")
        with engine.connect() as connection:
            assert "regime_history" not in sa.inspect(connection).get_table_names()
            assert "journal_entries" in sa.inspect(connection).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert "regime_history" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
