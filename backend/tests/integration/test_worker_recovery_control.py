"""Owner recovery restores supervision only; failures retain durable inhibition."""

import asyncio

import pytest
import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.trading.recover import recover_worker
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials"]
BODY = {
    "action": "RECOVER_WORKER", "confirmation": "RECOVER PAPER WORKER",
    "reason": "Owner reviewed failed storage and recovery evidence",
}


async def test_authenticated_recovery_retains_kill_and_requires_separate_review(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, _provider, api = await setup_worker(credentials, fake_clock, tmp_path)
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
    try:
        controls = EmergencyControls(fake_clock)
        await controls.activate("KILL", actor="owner", reason="Owner retains kill during recovery")
        token = api.headers.pop("Authorization")
        assert (await api.post("/api/v1/emergency", json=BODY)).status_code == 401
        api.headers["Authorization"] = token
        assert (await api.post("/api/v1/emergency", json=BODY | {
            "confirmation": "REVIEW PAPER WORKER"
        })).status_code == 422
        with monkeypatch.context() as scoped:
            scoped.setattr(credentials[0], "trading_mode", TradingMode.SUPERVISED)
            assert (await api.post("/api/v1/emergency", json=BODY)).status_code == 423
        worker.executor.ready = False
        response = await api.post("/api/v1/emergency", json=BODY)
        assert response.status_code == 200, response.text
        assert response.json()["kill_switch"] and response.json()["entries_blocked"]
        assert worker.executor.ready and worker.failed
        assert worker.last_cycle is None
        assert not get_trading_gate().new_entries_allowed
        assert await worker.executor.broker.list_orders() == []
    finally:
        await worker.stop()
        await api.aclose()


async def test_worker_stopping_while_recovery_waits_is_not_reactivated(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, _provider, api = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        await worker.cycle_lock.acquire()
        recovery = asyncio.create_task(recover_worker(worker, actor="owner", reason=BODY["reason"]))
        await asyncio.sleep(0)
        worker.running = False
        worker.cycle_lock.release()
        with pytest.raises(SafetyError, match="RUNNING_WORKER_REQUIRED"):
            await recovery
        assert not worker.running
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_WORKER_RECOVERED"
            )) == 0
    finally:
        if worker.cycle_lock.locked():
            worker.cycle_lock.release()
        await worker.stop()
        await api.aclose()


@pytest.mark.parametrize("failure", ["protection", "receipt", "cancellation"])
async def test_failed_recovery_never_reports_ready_or_releases_entries(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, failure
):
    worker, _provider, api = await setup_worker(credentials, fake_clock, tmp_path)
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)
    try:
        if failure == "cancellation":
            entered = asyncio.Event()

            async def interrupted():
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(worker.executor, "verify_protection", interrupted)
            recovery = asyncio.create_task(recover_worker(
                worker, actor="owner", reason=BODY["reason"]
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                recovery.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await recovery
            finally:
                if not recovery.done():
                    recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
        elif failure == "protection":
            async def refuse(*args, **kwargs):
                raise SafetyError("ISOLATED_PROTECTION_UNAVAILABLE")
            monkeypatch.setattr(worker.executor, "verify_protection", refuse)
            await api.post("/api/v1/emergency", json=BODY)
        else:
            original = AuditService.append_in_session

            async def refuse_receipt(self, session, identity, details, **kwargs):
                if identity.event_type == "PAPER_WORKER_RECOVERED":
                    raise RuntimeError("isolated audit storage interruption")
                return await original(self, session, identity, details, **kwargs)

            monkeypatch.setattr(AuditService, "append_in_session", refuse_receipt)
            with pytest.raises(RuntimeError, match="isolated audit"):
                await api.post("/api/v1/emergency", json=BODY)
        assert not worker.executor.ready and worker.failed
        assert not get_trading_gate().new_entries_allowed
        assert (await EmergencyControls(fake_clock).restore())["entries_blocked"]
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "PAPER_WORKER_RECOVERED"
            )) == 0
    finally:
        await worker.stop()
        await api.aclose()
