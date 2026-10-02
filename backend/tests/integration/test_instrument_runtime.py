from datetime import datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.config import get_settings
from app.core.calendar import TradingCalendar
from app.core.clock import IST
from app.core.enums import HealthStatus
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument_snapshot import InstrumentMasterSnapshot
from app.instruments.loader import InstrumentLoader
from app.instruments.runtime import InstrumentRefreshCheck, InstrumentRuntime

CSV = (
    "exchange,segment,trading_symbol,instrument_type,lot_size,tick_size,"
    "buy_allowed,sell_allowed,is_reserved\nNSE,CASH,TEST,EQ,1,0.01,1,1,0\n"
)


def runtime(fake_clock, handler):
    settings = get_settings().model_copy(update={"instrument_refresh_enabled": True})
    loader = InstrumentLoader(settings, transport=httpx.MockTransport(handler), clock=fake_clock)
    instance = InstrumentRuntime(
        settings, loader=loader, clock=fake_clock, calendar=TradingCalendar(complete_years=[2026])
    )
    instance.running = True
    return instance


async def test_real_loader_scheduler_restart_uses_persisted_snapshot(db_engine, fake_clock):
    fake_clock.set_to(datetime(2026, 1, 5, 8, 30, tzinfo=IST))
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, text=CSV)

    first = runtime(fake_clock, response)
    await first.cycle()
    assert first.status == "CURRENT"
    assert (await InstrumentRefreshCheck(first).run())[0] == HealthStatus.PASS
    restored = runtime(fake_clock, response)
    await restored.cycle()
    assert restored.status == "CURRENT"
    assert len(requests) == 1
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(InstrumentMasterSnapshot))
            == 1
        )
    fake_clock.advance(timedelta(days=1))
    await restored.cycle()
    assert len(requests) == 2


@pytest.mark.parametrize("hour", [7, 9, 12])
async def test_refresh_never_catches_up_during_trading(db_engine, fake_clock, hour):
    fake_clock.set_to(datetime(2026, 1, 5, hour, 30, tzinfo=IST))

    def unexpected(request):
        pytest.fail("must not download outside maintenance window")

    instance = runtime(fake_clock, unexpected)
    await instance.cycle()
    assert instance.status in {"AWAITING_PREOPEN", "REFRESH_MISSED"}
    assert (await InstrumentRefreshCheck(instance).run())[0] == HealthStatus.FAIL


async def test_failed_refresh_is_audited_and_not_retried_each_minute(db_engine, fake_clock):
    fake_clock.set_to(datetime(2026, 1, 5, 8, 30, tzinfo=IST))
    requests = []

    def failure(request):
        requests.append(request)
        return httpx.Response(503)

    instance = runtime(fake_clock, failure)
    await instance.cycle()
    await instance.cycle()
    assert instance.status == "FAILED"
    assert len(requests) == 1

    restored = runtime(fake_clock, failure)
    await restored.cycle()
    assert restored.status == "FAILED"
    assert len(requests) == 1

    async with db_session.session_scope() as session:
        row = await session.scalar(sa.select(AuditEvent))
        assert row.event_type == "INSTRUMENT_REFRESH_FAILED"


async def test_scheduler_is_opt_in_and_stops_cleanly(db_engine, fake_clock):
    disabled = InstrumentRuntime(get_settings(), clock=fake_clock)
    await disabled.start()
    assert not disabled.scheduler.running
    assert (await InstrumentRefreshCheck(disabled).run())[0] == HealthStatus.SKIPPED
    enabled = runtime(fake_clock, lambda request: httpx.Response(200, text=CSV))
    enabled.running = False
    await enabled.start()
    assert enabled.scheduler.running
    assert len(enabled.scheduler.get_jobs()) == 1
    await enabled.stop()
    assert not enabled.running
    assert enabled.status == "STOPPED"
