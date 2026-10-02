"""Application boot, health endpoints and migrations.

Covers BE-001, BE-002, BE-003, BE-010, DB-003, DEPLOY-005, MON-004.

These exercise the real ASGI app through its real lifespan: settings load,
logging configures, the mode resolves, the engine opens, startup checks run and
the gate is set from the result.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.models import metadata

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("authenticated_api")]


def _client(settings) -> TestClient:
    from app.main import create_app

    return TestClient(create_app(settings), raise_server_exceptions=False)


async def _create_schema(*, with_instruments: bool = False) -> None:
    from app.db import session as db_session

    engine = db_session.init_engine(force=True)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)

    if with_instruments:
        # The instruments check is critical: without lot and tick sizes the
        # system cannot size or price an order, so an empty master blocks trading.
        from decimal import Decimal

        from app.core.enums import Exchange, InstrumentType, Segment
        from app.db.models.instrument import Instrument

        async with db_session.session_scope() as session:
            session.add(
                Instrument(
                    exchange=Exchange.NSE,
                    segment=Segment.CASH,
                    instrument_type=InstrumentType.EQUITY,
                    trading_symbol="RELIANCE",
                    lot_size=1,
                    tick_size=Decimal("0.05"),
                )
            )

    await db_session.dispose_engine()


# --- BE-001 ---------------------------------------------------------------


async def test_app_boots_openapi(settings_env) -> None:
    settings = settings_env(STARTING_CAPITAL="500000")
    await _create_schema()

    with _client(settings) as client:
        response = client.get("/api/v1/openapi.json")
        assert response.status_code == 200

        schema = response.json()
        assert schema["info"]["title"].endswith("API")
        paths = schema["paths"]
        for expected in (
            "/api/v1/health/live",
            "/api/v1/health/ready",
            "/api/v1/system/status",
            "/api/v1/system/config",
        ):
            assert expected in paths, expected


# --- BE-002 ---------------------------------------------------------------


async def test_health_endpoints(settings_env) -> None:
    settings = settings_env(STARTING_CAPITAL="500000")
    await _create_schema(with_instruments=True)

    with _client(settings) as client:
        live = client.get("/api/v1/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "alive"

        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["trading_allowed"] is False
        assert "system_clock" in body["blocking_reason"]
        assert "market_data" in body["blocking_reason"]
        names = {check["name"] for check in body["checks"]}
        assert {
            "database",
            "redis",
            "risk_config",
            "mode",
            "groww_auth",
            "instruments",
            "market_status",
        } <= names
        # The §10 checks not yet built must be reported, never implied to pass.
        assert body["missing_checks"] == []
        orders = next(check for check in body["checks"] if check["name"] == "order_service")
        assert orders["status"] == "SKIPPED"
        news = next(check for check in body["checks"] if check["name"] == "news")
        assert news["status"] == "DEGRADED"
        assert news["critical"] is False

        # PAPER mode needs no Groww credentials, so that check skips rather than
        # failing — a red check nobody needs teaches operators to ignore red.
        groww = next(c for c in body["checks"] if c["name"] == "groww_auth")
        assert groww["status"] == "SKIPPED"


@pytest.mark.safety
async def test_ready_returns_503_when_a_critical_check_fails(settings_env) -> None:
    """MON-004: no capital configured means no sizing, so trading is disabled."""
    settings = settings_env(STARTING_CAPITAL=None)
    await _create_schema()

    with _client(settings) as client:
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["trading_allowed"] is False
        assert "risk_config" in body["blocking_reason"]

        failing = next(c for c in body["checks"] if c["name"] == "risk_config")
        assert failing["status"] == "FAIL"
        assert failing["critical"] is True


async def test_health_reports_a_missing_schema_rather_than_passing(settings_env) -> None:
    settings = settings_env(STARTING_CAPITAL="500000")
    # Deliberately do NOT create the schema.
    with _client(settings) as client:
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 503
        database = next(c for c in ready.json()["checks"] if c["name"] == "database")
        assert database["status"] == "FAIL"
        assert "alembic upgrade head" in database["detail"]


# --- BE-003 ---------------------------------------------------------------


async def test_system_status(settings_env) -> None:
    settings = settings_env(STARTING_CAPITAL="500000", APP_NAME="ATS-test")
    await _create_schema(with_instruments=True)

    with _client(settings) as client:
        body = client.get("/api/v1/system/status").json()

        assert body["app_name"] == "ATS-test"
        assert body["trading_mode"] == "PAPER"
        assert body["broker_provider"] == "paper"
        # No Anthropic key is configured, so the effective provider degrades.
        assert body["llm_provider"] == "fallback"
        assert body["gate"]["trading_enabled"] is True
        assert body["uptime_seconds"] is not None
        assert body["server_time_ist"].endswith("+05:30")
        assert body["shutting_down"] is False


async def test_config_endpoint_redacts_secrets(settings_env) -> None:
    settings = settings_env(
        STARTING_CAPITAL="500000",
        GROWW_API_KEY="do-not-leak-this",
        ANTHROPIC_API_KEY="sk-ant-do-not-leak",
    )
    await _create_schema()

    with _client(settings) as client:
        raw = client.get("/api/v1/system/config").text

    assert "do-not-leak-this" not in raw
    assert "sk-ant-do-not-leak" not in raw
    assert "***SET***" in raw


# --- BE-010 ---------------------------------------------------------------


async def test_correlation_id_propagation(settings_env) -> None:
    settings = settings_env(STARTING_CAPITAL="500000")
    await _create_schema()

    with _client(settings) as client:
        generated = client.get("/api/v1/health/live")
        assert generated.headers["X-Request-Id"].startswith("cor_")

        echoed = client.get("/api/v1/health/live", headers={"X-Request-Id": "cor_supplied"})
        assert echoed.headers["X-Request-Id"] == "cor_supplied"


# --- DB-003 ---------------------------------------------------------------


@pytest.mark.parametrize("filename", ["migrated.db", "percent%20path.db"])
def test_alembic_upgrade_creates_the_schema(settings_env, tmp_path, filename) -> None:
    """The migration itself must produce a working schema, not just the models."""
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from tests.conftest import BACKEND_ROOT

    db_path = tmp_path / filename
    settings_env(DATABASE_URL=f"sqlite+aiosqlite:///{db_path}")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()

    missing = set(metadata.tables) - tables
    assert not missing, f"migration did not create: {sorted(missing)}"
    assert "alembic_version" in tables


def test_alembic_downgrade_removes_the_schema(settings_env, tmp_path) -> None:
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from tests.conftest import BACKEND_ROOT

    db_path = tmp_path / "roundtrip.db"
    settings_env(DATABASE_URL=f"sqlite+aiosqlite:///{db_path}")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()

    assert not (set(metadata.tables) & tables)
