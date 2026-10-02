"""Actual costed PAPER trades drive persistent strategy stand-down and owner review."""

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.journal import JournalEntry
from app.db.models.trading import Position
from app.execution.paper import require_enabled_strategy
from app.portfolio.cost_store import CostStore
from app.strategies import monitor
from app.strategies.monitor import enforce, monitor_all
from app.strategies.registry import StrategyRegistry
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.unit.test_costs import schedule

__all__ = ["credentials"]


async def closed_trade(credentials, fake_clock, *, costs=True, winning=False):
    executor, proposal, market, client, context = await setup_execution(
        credentials, fake_clock, risk_cost=Decimal("0.5")
    )
    if costs:
        await CostStore(fake_clock).publish(
            schedule(), actor="owner", reason="Isolated test tariff"
        )
    await executor.submit(proposal)
    async with db_session.session_scope() as session:
        position = await session.scalar(sa.select(Position))
    fake_clock.advance(timedelta(seconds=1))
    price = Decimal(104 if winning else 99)
    market.update(bid=price, ask=price + Decimal("0.05"), observed=fake_clock.now())
    await executor.exit(position.id)
    return executor, proposal, client, context


def policy(context, *, version=1, threshold="100"):
    return {
        "version": context.strategy.version,
        "reason": "Owner declares explicit test monitoring",
        "policy": {
            "version": version,
            "window_trades": 10,
            "minimum_trades": 1,
            "maximum_drawdown_amount": threshold,
            "data_origin": context.market.data_origin.value,
        },
    }


async def test_real_trade_disables_once_survives_restart_and_requires_healthy_reset(
    db_engine, credentials, fake_clock
):
    executor, proposal, client, context = await closed_trade(credentials, fake_clock)
    path = f"/api/v1/strategies/{context.strategy.id}/degradation"
    try:
        response = await client.post(path + "/policy", json=policy(context))
        assert response.status_code == 200, response.text
        await monitor_all(clock=fake_clock)
        result = (await client.get(path, params={"version": context.strategy.version})).json()
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            planned = await session.get(Proposal, proposal)
        assert journal.gross_pnl == -200 and journal.charges > 0
        assert result["status"] == "BREACHED" and result["auto_disabled"]
        assert Decimal(result["maximum_drawdown_amount"]) == -journal.net_pnl
        assert result["journal_ids"] == [journal.id]
        await enforce(context.strategy.id, context.strategy.version, clock=fake_clock)
        assert (
            await StrategyRegistry().get(context.strategy.id, context.strategy.version)
        ).auto_disabled
        with pytest.raises(SafetyError, match="STRATEGY_DISABLED"):
            await require_enabled_strategy(planned, executor.settings, clock=fake_clock)
        enabled = await client.post(
            f"/api/v1/strategies/{context.strategy.id}/paper",
            json={
                "version": context.strategy.version,
                "enabled": True,
                "reason": "Attempted unsafe re-enable",
            },
        )
        assert enabled.status_code == 409
        reset_body = {
            "version": context.strategy.version,
            "policy_event_id": result["policy_event_id"],
            "last_event_id": result["last_event_id"],
            "reason": "Explicit owner review of results",
            "confirmation": "RESET STRATEGY DEGRADATION",
        }
        assert (await client.post(path + "/reset", json=reset_body)).status_code == 409
        assert (
            await client.post(path + "/policy", json=policy(context, version=2, threshold="1000"))
        ).status_code == 200
        healthy = (await client.get(path, params={"version": context.strategy.version})).json()
        assert healthy["status"] == "HEALTHY" and healthy["auto_disabled"]
        assert (await client.post(path + "/reset", json=reset_body)).status_code == 409
        reset_body["policy_event_id"] = healthy["policy_event_id"]
        assert (await client.post(path + "/reset", json=reset_body)).status_code == 200
        row = await StrategyRegistry().get(context.strategy.id, context.strategy.version)
        assert not row.auto_disabled and not row.enabled_paper
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "STRATEGY_AUTO_DISABLED")
                )
                == 1
            )
            notices = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "NOTIFICATION_REQUESTED"
                        )
                    )
                ).all()
            )
            assert (
                sum(
                    event.result["notification"]["event_type"] == "STRATEGY_AUTO_DISABLED"
                    for event in notices
                )
                == 1
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize("case", ["missing_costs", "corrupt", "future_trade", "healthy"])
async def test_monitor_refuses_unavailable_evidence_and_avoids_lookahead(
    db_engine, credentials, fake_clock, case
):
    _executor, _proposal, client, context = await closed_trade(
        credentials, fake_clock, costs=case != "missing_costs", winning=case == "healthy"
    )
    path = f"/api/v1/strategies/{context.strategy.id}/degradation"
    try:
        assert (await client.post(path + "/policy", json=policy(context))).status_code == 200
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
        async with db_engine.begin() as connection:
            if case == "corrupt":
                await connection.execute(
                    sa.update(JournalEntry.__table__)
                    .where(JournalEntry.id == journal.id)
                    .values(net_pnl=Decimal(500))
                )
            elif case == "future_trade":
                await connection.execute(
                    sa.update(JournalEntry.__table__)
                    .where(JournalEntry.id == journal.id)
                    .values(closed_at=fake_clock.now() + timedelta(hours=1))
                )
        result = await enforce(context.strategy.id, context.strategy.version, clock=fake_clock)
        if case in {"missing_costs", "corrupt"}:
            assert result.status == "UNAVAILABLE" and result.auto_disabled
            assert result.net_pnl is None and result.maximum_drawdown_amount is None
        else:
            assert not result.auto_disabled
            assert result.status == ("HEALTHY" if case == "healthy" else "WARMING_UP")
    finally:
        await client.aclose()


async def test_preflight_monitor_and_atomic_notification_failure(
    db_engine, credentials, fake_clock, monkeypatch
):
    executor, proposal, client, context = await closed_trade(credentials, fake_clock)
    path = f"/api/v1/strategies/{context.strategy.id}/degradation"
    try:
        assert (await client.post(path + "/policy", json=policy(context))).status_code == 200
        async with db_session.session_scope() as session:
            planned = await session.get(Proposal, proposal)
        original = monitor.enqueue

        async def failed_enqueue(*args, **kwargs):
            raise OSError("isolated outbox storage failure")

        monkeypatch.setattr(monitor, "enqueue", failed_enqueue)
        with pytest.raises(OSError, match="outbox storage failure"):
            await require_enabled_strategy(planned, executor.settings, clock=fake_clock)
        row = await StrategyRegistry().get(context.strategy.id, context.strategy.version)
        assert not row.auto_disabled and row.enabled_paper
        async with db_session.session_scope() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "STRATEGY_AUTO_DISABLED")
                )
                == 0
            )
        monkeypatch.setattr(monitor, "enqueue", original)
        with pytest.raises(SafetyError, match="STRATEGY_AUTO_DISABLED"):
            await require_enabled_strategy(planned, executor.settings, clock=fake_clock)
        assert len(await executor.broker.list_orders()) == 2
        client.headers.pop("Authorization")
        assert (
            await client.post(path + "/policy", json=policy(context, version=2))
        ).status_code == 401
        assert (
            await client.post(
                path + "/reset",
                json={
                    "version": context.strategy.version,
                    "policy_event_id": "untrusted",
                    "last_event_id": "untrusted",
                    "reason": "Unauthenticated reset attempt",
                    "confirmation": "RESET STRATEGY DEGRADATION",
                },
            )
        ).status_code == 401
    finally:
        await client.aclose()
