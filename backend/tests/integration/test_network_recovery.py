"""A real TCP outage must block PAPER execution until explicit owner recovery."""

import asyncio
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.trading import Order, Position, Trade
from app.monitoring.gate import get_trading_gate
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.network_proxy import NetworkProxy
from tests.process_database import process_database

__all__ = ["credentials"]


@pytest.mark.skipif(not os.environ.get("ATS_TEST_POSTGRES_URL"), reason="set ATS_TEST_POSTGRES_URL")
async def test_database_network_outage_and_owner_recovery(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, api = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with process_database(worker.settings, "postgres", monkeypatch):
            target = make_url(worker.settings.database_url)
            assert target.database.startswith("ats_recovery_test_")
            proxy = NetworkProxy(target.host, target.port or 5432)
            engine = None
            try:
                await proxy.start()
                proxied = target.set(host="127.0.0.1", port=proxy.listen_port)
                engine = create_async_engine(proxied, **db_session._engine_kwargs(worker.settings))
                with monkeypatch.context() as scoped:
                    scoped.setattr(db_session, "_sessionmaker", async_sessionmaker(
                        engine, expire_on_commit=False, autoflush=False
                    ))
                    await worker.executor.monitor_once()
                    async with db_session.session_scope() as session:
                        original = await session.scalar(sa.select(Order))
                        before = (await session.execute(sa.select(
                            Position.net_quantity, Position.realised_pnl, Position.total_charges
                        ))).one()
                    await proxy.disconnect()
                    with pytest.raises(OSError):
                        await asyncio.wait_for(worker.executor.monitor_once(), timeout=10)
                    assert not worker.executor.ready
                    assert not get_trading_gate().new_entries_allowed
                    with pytest.raises(SafetyError, match="RECOVERY"):
                        await worker.executor.submit(original.proposal_id)
                    await proxy.start()
                    response = await api.post("/api/v1/emergency", json={
                        "action": "RECOVER_WORKER", "confirmation": "RECOVER PAPER WORKER",
                        "reason": "Owner reviewed isolated TCP interruption and restored connectivity",
                    })
                    assert response.status_code == 200, response.text
                    assert response.json()["entries_blocked"]
                    assert worker.executor.ready and worker.failed
                    async with db_session.session_scope() as session:
                        assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
                        assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
                        assert (await session.scalar(sa.select(Position))).net_quantity == 111
                        after = (await session.execute(sa.select(
                            Position.net_quantity, Position.realised_pnl, Position.total_charges
                        ))).one()
                        assert after == before
            finally:
                if engine is not None:
                    await engine.dispose()
                await proxy.disconnect()
    finally:
        await worker.stop()
        await api.aclose()
