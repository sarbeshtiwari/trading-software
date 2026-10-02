"""Synthetic fixtures exercise actual pipeline, broker, durable OMS and API."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.brokers.paper.account import PaperAccount
from app.brokers.paper.engine import FillConfig
from app.brokers.paper.state import PaperStateStore
from app.core.data_origin import DataOrigin
from app.core.enums import ExitReason, OrderStatus
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.execution.paper import PaperExecution
from app.main import create_app
from app.marketdata.models import DepthLevel, Quote
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.risk.safety import RiskSafety
from app.security.auth import OwnerAuth
from app.strategies.registry import StrategyRegistry
from tests.integration.test_auth import credentials
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


async def setup_execution(credentials, fake_clock, *, risk_cost=Decimal(0), fill_config=None):
    fake_clock.set_to(OBSERVED)
    context = await setup_context()
    context = context.model_copy(
        update={
            "costs": context.costs.model_copy(update={"risk_cost_per_unit": risk_cost}),
        }
    )
    settings, password = credentials
    settings.starting_capital = Decimal("100000")
    access, _ = await OwnerAuth().login("owner", password)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {access}"},
    )
    response = await client.post(
        "/api/v1/risk/configuration",
        json={
            "limits": context.limits.model_dump(mode="json"),
            "expected_version": 0,
            "reason": "Owner activated isolated execution test configuration",
        },
    )
    assert response.status_code == 200, response.text
    market = {
        "bid": Decimal("99.95"),
        "ask": Decimal("100"),
        "quantity": 10000,
        "observed": fake_clock.now(),
    }

    async def source(instrument):
        quantity = market["quantities"].pop(0) if market.get("quantities") else market["quantity"]
        return Quote(
            instrument,
            market["ask"],
            market["observed"],
            DataOrigin.SYNTHETIC,
            bids=(DepthLevel(market["bid"], quantity),),
            asks=market.get("asks", (DepthLevel(market["ask"], quantity),)),
            lower_circuit=market.get("lower_circuit"),
            upper_circuit=market.get("upper_circuit"),
        )

    engine = PaperExecution(
        source,
        settings=settings,
        clock=fake_clock,
        fill_config=fill_config or FillConfig(slippage_bps=Decimal(0), latency_ms=0),
    )
    await engine.recover()
    outcome = await quant_decision(DecisionPipeline(validator()), payload(), context)
    assert outcome.approved_quantity == int(Decimal(500) / (Decimal(2) + risk_cost))
    return engine, outcome.proposal_id, market, client, context


@pytest.mark.parametrize("split", [False, True])
async def test_real_pipeline_fill_exit_journal_and_dashboard(
    db_engine, credentials, fake_clock, split
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        if split:
            market["asks"] = (DepthLevel(Decimal("99.95"), 1), DepthLevel(Decimal("100"), 10000))
        identifier = await engine.submit(proposal)
        assert await engine.submit(proposal) == identifier
        assert len(await engine.broker.list_orders()) == 1
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 250
            assert position.average_price == Decimal("99.9998" if split else "100")
            assert position.stop_loss_price == 98 and not position.is_protected
        assert "PAPER_POSITION_REQUIRES_MONITORING" in get_trading_gate().reason()
        market.update(bid=Decimal("104"), ask=Decimal("104.05"))
        market.pop("asks", None)
        await engine.monitor_once()
        exit_id = await engine.exit(position.id, ExitReason.TARGET)
        assert await engine.exit(position.id, ExitReason.TARGET) == exit_id
        async with db_session.session_scope() as session:
            position = await session.get(Position, position.id)
            journal = await session.scalar(sa.select(JournalEntry))
            expected = Decimal("1000.05" if split else "1000")
            assert position.net_quantity == 0 and position.realised_pnl == expected
            assert engine.broker.account.realised_pnl == expected
            assert journal.gross_pnl == expected and journal.net_pnl is None
            assert journal.charges is None and journal.entry_order_id == identifier
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == (
                3 if split else 2
            )
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200, response.text
        assert len(response.json()["orders"]) == 2
        assert len(response.json()["preflights"]) == 1
        assert response.json()["journal"][0]["net_pnl"] is None
        assert Decimal(response.json()["account"]["realised_pnl"]) == expected
        assert response.json()["positions"][0]["mode"] == "PAPER"
        assert response.json()["positions"][0]["execution_realism"] == "SIMULATED"
        assert response.json()["positions"][0]["marked_at"].endswith("Z")
        constraints = [
            fill["cost_breakdown"]["instrument_constraints"] for fill in response.json()["fills"]
        ]
        assert all(item["instrument_id"] == position.instrument_id for item in constraints)
        assert all(
            item["lot_size"] == 1 and Decimal(item["tick_size"]) == Decimal("0.05")
            for item in constraints
        )
    finally:
        await client.aclose()


async def test_dispatch_latch_and_stale_quote_block_broker(db_engine, credentials, fake_clock):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        market["observed"] -= timedelta(seconds=60)
        with pytest.raises(SafetyError, match="STALE_OR_INVALID"):
            await engine.submit(proposal)
        market["observed"] = fake_clock.now()
        await RiskSafety().trip_error("PAPER", "SYNTHETIC")
        with pytest.raises(ValueError, match="latch"):
            await engine.submit(proposal)
        assert await engine.broker.list_orders() == []
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_loss_price", None),
        ("stop_loss_price", Decimal("97")),
        ("target_price", None),
        ("net_quantity", 251),
    ],
)
async def test_watchdog_detects_lost_protection_after_restart(
    db_engine, credentials, fake_clock, field, value
):
    engine, proposal, _market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            setattr(position, field, value)
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        if field == "net_quantity":
            with pytest.raises(SafetyError, match="RECONCILE"):
                await restarted.recover()
        else:
            await restarted.recover()
        watcher = engine if field == "net_quantity" else restarted
        with pytest.raises(SafetyError, match="PROTECTION_"):
            if field == "net_quantity":
                await watcher.verify_protection()
            else:
                await watcher.monitor_once()
        events = await AuditService().chain(position.id)
        assert events[-1].event_type == "PROTECTION_FAILURE"
        assert await AuditService().verify(position.id)
        state = await RiskSafety().restore(TradingMode.PAPER, DataOrigin.SYNTHETIC)
        assert state.engine_error
        async with db_session.session_scope() as session:
            stored = await session.get(Position, position.id)
            if field != "net_quantity":
                assert stored.net_quantity == 0
                assert stored.exit_reason == ExitReason.EMERGENCY
            else:
                assert stored.net_quantity == 251
                assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
    finally:
        await client.aclose()


async def test_watchdog_stale_quote_never_fabricates_emergency_fill(
    db_engine, credentials, fake_clock
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            position.stop_loss_price = None
        market["observed"] -= timedelta(seconds=60)
        with pytest.raises(SafetyError):
            await engine.monitor_once()
        async with db_session.session_scope() as session:
            stored = await session.get(Position, position.id)
            assert stored.net_quantity == 250
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 1
        assert (await RiskSafety().restore("PAPER", "SYNTHETIC")).engine_error
        assert await AuditService().verify(position.id)
    finally:
        await client.aclose()


async def test_restart_partial_order_and_position_recovery(db_engine, credentials, fake_clock):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        market["quantities"] = [10000, 10000, 100]
        identifier = await engine.submit(proposal)
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        await restarted.recover()
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            position = await session.scalar(sa.select(Position))
            assert order.status == OrderStatus.PARTIALLY_FILLED and position.net_quantity == 100
        assert await restarted.submit(proposal) == identifier
        market.update(quantity=10000, bid=Decimal("98"), ask=Decimal("98.05"))
        await restarted.exit(position.id, ExitReason.EMERGENCY)
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.CANCELLED
            assert (await session.get(Position, position.id)).realised_pnl == Decimal("-200")
            assert await session.scalar(sa.select(sa.func.count()).select_from(Trade)) == 2
    finally:
        await client.aclose()


async def test_timeout_after_acceptance_looks_up_without_resubmit(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    calls = []
    original = engine.broker.place_order

    async def lost_ack(request):
        calls.append(request.reference_id)
        await original(request)
        raise TimeoutError("isolated lost acknowledgement")

    monkeypatch.setattr(engine.broker, "place_order", lost_ack)
    try:
        identifier = await engine.submit(proposal)
        assert await engine.submit(proposal) == identifier and len(calls) == 1
        async with db_session.session_scope() as session:
            assert (await session.get(Order, identifier)).status == OrderStatus.EXECUTED
    finally:
        await client.aclose()


@pytest.mark.parametrize("price,drawdown", [("90", False), ("50", True)])
async def test_actual_loss_monitor_latches_and_exits(
    db_engine, credentials, fake_clock, price, drawdown
):
    engine, proposal, market, client, _ = await setup_execution(credentials, fake_clock)
    try:
        await engine.submit(proposal)
        market.update(bid=Decimal(price), ask=Decimal(price))
        await engine.monitor_once()
        state = await RiskSafety().restore("PAPER", "SYNTHETIC")
        assert state.daily_loss and state.drawdown == drawdown
        assert not get_trading_gate().new_entries_allowed
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 0
            assert position.exit_reason == ExitReason.STOP_LOSS
            assert position.realised_pnl == (Decimal(price) - 100) * 250
    finally:
        await client.aclose()


async def test_broker_failure_unknown_is_durable_and_not_retried(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal, _, client, _ = await setup_execution(credentials, fake_clock)
    calls = []

    async def unavailable(request):
        calls.append(request)
        raise ConnectionError("isolated broker failure")

    monkeypatch.setattr(engine.broker, "place_order", unavailable)
    try:
        with pytest.raises(SafetyError, match="NO_BLIND_RESUBMISSION"):
            await engine.submit(proposal)
        async with db_session.session_scope() as session:
            order = await session.scalar(sa.select(Order))
            assert order.status == OrderStatus.UNKNOWN
        assert await engine.submit(proposal) == order.id and len(calls) == 1
        restarted = PaperExecution(engine.source, settings=engine.settings, clock=fake_clock)
        with pytest.raises(SafetyError, match="NO_BLIND_RESUBMISSION"):
            await restarted.recover()
        assert not get_trading_gate().new_entries_allowed
        assert await restarted.broker.list_orders() == []
    finally:
        await client.aclose()


async def test_durable_paper_snapshot_rejects_stale_writer(db_engine):
    first = PaperStateStore()
    await first.save({"account": {"cash": "0"}})
    second = PaperStateStore()
    await second.load()
    await first.save({"account": {"cash": "1"}})
    with pytest.raises(SafetyError, match="Concurrent"):
        await second.save({"account": {"cash": "999"}})
    assert (await PaperStateStore().load())["account"]["cash"] == "1"
    restored = PaperAccount.from_dict({"starting_capital": "100000", "cash": "0"})
    assert restored.cash == 0


async def test_invalid_approval_disabled_strategy_and_database_failure_never_submit(
    db_engine, credentials, fake_clock, monkeypatch
):
    engine, proposal_id, _, client, context = await setup_execution(credentials, fake_clock)
    try:
        await StrategyRegistry().set_enabled(
            context.strategy.id, context.strategy.version, TradingMode.PAPER, False
        )
        with pytest.raises(SafetyError, match="STRATEGY_DISABLED"):
            await engine.submit(proposal_id)
        await StrategyRegistry().set_enabled(
            context.strategy.id, context.strategy.version, TradingMode.PAPER, True
        )
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, proposal_id)
            proposal.entry_price = Decimal("110")
        with pytest.raises(SafetyError, match="LINEAGE"):
            await engine.submit(proposal_id)
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, proposal_id)
            proposal.entry_price = Decimal("100")

        async def unavailable(*args, **kwargs):
            raise OperationalError("isolated test database failure", {}, Exception())

        monkeypatch.setattr(engine, "_audit", unavailable)
        with pytest.raises(OperationalError):
            await engine.submit(proposal_id)
        assert not engine.ready and not get_trading_gate().new_entries_allowed
        assert await engine.broker.list_orders() == []
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
    finally:
        await client.aclose()
