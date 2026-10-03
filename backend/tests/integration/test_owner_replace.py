"""Replacement uses real canonical approvals and the durable PAPER broker."""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.api import orders as orders_api
from app.audit.service import AuditService
from app.brokers.paper.engine import FillConfig
from app.core.calendar import TradingCalendar
from app.core.enums import OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.trading import Order
from app.execution import replacement as replacement_service
from app.execution.paper import PaperExecution
from app.execution.replacement_state import recover_replacements
from app.monitoring.gate import get_trading_gate
from app.risk.safety import RiskSafety
from app.trading.worker import PaperWorker
from tests.integration.test_paper_execution import credentials, setup_execution
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


@pytest.mark.parametrize(
    "failure", [None, "cancel", "interrupted", "risk_after_cancel", "accepted_interrupt",
                "owner_interrupted", "owner_accepted_interrupt"]
)
async def test_replace_unfilled_entry_uses_new_approval_and_replays(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, failure
):
    engine, proposal, _market, client, context = await setup_execution(
        credentials,
        fake_clock,
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000),
    )
    replacement = await quant_decision(
        DecisionPipeline(validator()), payload(entry="100.05", target="104.15"), context
    )
    assert replacement.approved_quantity == 243
    identifier = await engine.submit(proposal)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "replacement.lock",
    )
    await worker.start(schedule=False)
    monkeypatch.setattr(orders_api, "active_worker", lambda: worker)
    monkeypatch.setattr("app.api.emergency.active_worker", lambda: worker)

    async def owner_recovery():
        response = await client.post("/api/v1/emergency", json={
            "action": "RECOVER_WORKER", "confirmation": "RECOVER PAPER WORKER",
            "reason": "Owner reviewed interrupted replacement after connection recovery",
        })
        assert response.status_code == 200, response.text
        assert response.json()["entries_blocked"]
        assert not get_trading_gate().new_entries_allowed

    try:
        body = {
            "request_id": str(uuid4()),
            "reason": "Replace isolated unfilled entry with fresh approval",
            "confirmation": "REPLACE PAPER ENTRY",
            "replacement_proposal_id": replacement.proposal_id,
        }
        if failure in {"accepted_interrupt", "owner_accepted_interrupt"}:
            monkeypatch.setattr(
                replacement_service,
                "finish_replacement",
                AsyncMock(side_effect=KeyboardInterrupt("after acceptance before receipt")),
            )
            with pytest.raises(KeyboardInterrupt):
                await replacement_service.replace_entry(
                    worker,
                    identifier,
                    replacement.proposal_id,
                    actor="owner",
                    request_id=body["request_id"],
                    reason=body["reason"],
                )
            assert len(await engine.broker.list_orders()) == 2
            if failure == "owner_accepted_interrupt":
                await owner_recovery()
                await owner_recovery()
                async with db_session.session_scope() as session:
                    receipts = list(await session.scalars(sa.select(AuditEvent).where(
                        AuditEvent.event_type == "ENTRY_REPLACE_RESULT",
                    )))
                    assert len(receipts) == 1
                    assert receipts[0].result["code"] == "INTERRUPTED_REVIEW_REQUIRED"
                    assert receipts[0].result["replacement_status"] == "OPEN"
                assert len(await engine.broker.list_orders()) == 2
                return
            await worker.stop()
            restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
            worker = PaperWorker(
                restored,
                calendar=TradingCalendar(complete_years=[2026]),
                lock_path=tmp_path / "replacement.lock",
            )
            await worker.start(schedule=False)
            async with db_session.session_scope() as session:
                receipt = await session.scalar(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "ENTRY_REPLACE_RESULT",
                    )
                )
                assert receipt.result["code"] == "INTERRUPTED_REVIEW_REQUIRED"
                assert receipt.result["replacement_order_id"] is not None
                assert receipt.result["replacement_status"] == "OPEN"
            await recover_replacements(restored)
            assert len(await restored.broker.list_orders()) == 2
            return
        if failure in {"interrupted", "owner_interrupted"}:
            original_prepare = replacement_service.prepare_replacement

            async def interrupted(*args, **kwargs):
                await original_prepare(*args, **kwargs)
                raise KeyboardInterrupt("simulated process interruption")

            monkeypatch.setattr(replacement_service, "prepare_replacement", interrupted)
            with pytest.raises(KeyboardInterrupt):
                await replacement_service.replace_entry(
                    worker,
                    identifier,
                    replacement.proposal_id,
                    actor="owner",
                    request_id=body["request_id"],
                    reason=body["reason"],
                )
            with pytest.raises(SafetyError, match="REPLACEMENT_AUTHORIZATION_REQUIRED"):
                await engine.submit(replacement.proposal_id)
            if failure == "owner_interrupted":
                await owner_recovery()
                await owner_recovery()
            else:
                await recover_replacements(engine)
                await recover_replacements(engine)
            async with db_session.session_scope() as session:
                reserved = await session.get(Proposal, replacement.proposal_id)
                assert reserved.status == "EXECUTION_BLOCKED"
            assert len(await engine.broker.list_orders()) == 1
            return
        if failure == "cancel":
            monkeypatch.setattr(engine, "cancel", AsyncMock(side_effect=RuntimeError("boundary")))
        if failure == "risk_after_cancel":
            cancel = engine.cancel

            async def cancel_then_disarm(identifier):
                await cancel(identifier)
                await RiskSafety().trip_error("PAPER", "TEST_AFTER_CANCELLATION")

            monkeypatch.setattr(engine, "cancel", cancel_then_disarm)
        response = await client.post(f"/api/v1/orders/{identifier}/replace", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        if failure == "risk_after_cancel":
            assert result["original_status"] == "CANCELLED"
            assert result["replacement_status"] == "NOT_SUBMITTED"
            assert result["error"] is not None
            assert len(await engine.broker.list_orders()) == 1
            return
        if failure == "cancel":
            assert result["replacement_status"] == "NOT_SUBMITTED"
            assert result["error"] == "CANCEL_NOT_SAFE_TO_REPLACE"
            assert len(await engine.broker.list_orders()) == 1
            assert not engine.gate.new_entries_allowed
            return
        assert result["original_status"] == "CANCELLED"
        assert result["replacement_status"] == "OPEN", result
        assert result["error"] is None
        repeated = await client.post(f"/api/v1/orders/{identifier}/replace", json=body)
        assert repeated.json() == result
        assert len(await engine.broker.list_orders()) == 2
        async with db_session.session_scope() as session:
            orders = list(await session.scalars(sa.select(Order)))
            assert len(orders) == 2
            updated = await session.get(Order, result["replacement_order_id"])
            assert updated.parent_order_id == identifier
            assert updated.quantity == 243
            assert updated.proposal_id == replacement.proposal_id
        assert await AuditService().verify(result["audit_chain_id"], expected_count=2)
        await worker.stop()
        restored = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        worker = PaperWorker(
            restored,
            calendar=TradingCalendar(complete_years=[2026]),
            lock_path=tmp_path / "replacement.lock",
        )
        await worker.start(schedule=False)
        assert len(await restored.broker.list_orders()) == 2
        assert await restored.submit(replacement.proposal_id) == result["replacement_order_id"]
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize("condition", ["stale", "filled", "unknown", "storage"])
async def test_replacement_refuses_before_cancelling(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch, condition
):
    engine, proposal, _market, client, context = await setup_execution(
        credentials,
        fake_clock,
        fill_config=FillConfig(slippage_bps=Decimal(0), latency_ms=2000),
    )
    replacement = await quant_decision(
        DecisionPipeline(validator()), payload(entry="100.05", target="104.15"), context
    )
    identifier = await engine.submit(proposal)
    worker = PaperWorker(
        engine,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "refusal.lock",
    )
    await worker.start(schedule=False)
    cancel = AsyncMock(wraps=engine.cancel)
    monkeypatch.setattr(engine, "cancel", cancel)
    try:
        if condition == "stale":
            fake_clock.advance_seconds(120)
        if condition in {"filled", "unknown"}:
            async with db_session.session_scope() as session:
                original = await session.get(Order, identifier)
                original.status = (
                    OrderStatus.EXECUTED if condition == "filled" else OrderStatus.UNKNOWN
                )
        if condition == "storage":
            append = AuditService.append_in_session

            async def fail_child(self, session, identity, payload, **kwargs):
                if identity.event_type == "ENTRY_CANCEL_INTENT":
                    raise RuntimeError("isolated storage failure")
                return await append(self, session, identity, payload, **kwargs)

            monkeypatch.setattr(AuditService, "append_in_session", fail_child)
        with pytest.raises(RuntimeError if condition == "storage" else SafetyError):
            await replacement_service.replace_entry(
                worker,
                identifier,
                replacement.proposal_id,
                actor="owner",
                request_id=str(uuid4()),
                reason="Isolated refusal acceptance",
            )
        cancel.assert_not_awaited()
        assert len(await engine.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            candidate = await session.get(Proposal, replacement.proposal_id)
            assert candidate.status == "RISK_APPROVED"
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.event_type == "ENTRY_REPLACE_INTENT",
                    )
                )
                == 0
            )
    finally:
        await worker.stop()
        await client.aclose()
