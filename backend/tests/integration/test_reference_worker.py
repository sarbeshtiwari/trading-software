"""Scheduled worker builds fresh context; only market data is an external fixture."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.proposal import TradeProposal
from app.analysis.regime.classifier import classify
from app.analysis.regime.events import EventCalendar
from app.audit.service import AuditService
from app.config import reload_settings
from app.core.calendar import TradingCalendar
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.journal import JournalEntry
from app.db.models.llm import LLMCall
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.portfolio.cost_store import CostStore
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry
from app.trading.inputs import ReferenceInputs, ReferenceInputStore
from app.trading.worker import PaperWorker
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_costs import schedule
from tests.unit.test_regime import inputs, policy

__all__ = ["credentials"]


async def setup_worker(
    credentials,
    fake_clock,
    tmp_path,
    missing=None,
    costed=False,
    origin=DataOrigin.SYNTHETIC,
    fill_config=None,
):
    executor, unused, _market, client, context = await setup_execution(
        credentials,
        fake_clock,
        risk_cost=Decimal("0.5") if costed else Decimal(0),
        fill_config=fill_config,
    )
    if costed:
        await CostStore(fake_clock).publish(
            schedule(), actor="fixture-owner", reason="Synthetic fee evidence"
        )
    now = fake_clock.now()
    calendar = EventCalendar(
        source="isolated-fixture-calendar",
        known_at=now,
        coverage_start=now - timedelta(hours=1),
        coverage_end=now + timedelta(hours=1),
        events=(),
    )
    decision = classify(
        inputs().model_copy(update={"calendar": calendar, "data_origin": origin}), policy()
    )
    async with db_session.session_scope() as session:
        (await session.get(Proposal, unused)).status = "FIXTURE_UNUSED"
        regime = await session.get(RegimeHistory, "reg-test")
        regime.data_origin = origin.value
        regime.decision = decision.model_dump(mode="json")
    strategy = ClosedCandleBreakout("ins-test", "NSE:TEST", "0.05", "0.005", clock=fake_clock)
    await StrategyRegistry().register(strategy, enabled_paper=True)
    if missing != "inputs":
        await ReferenceInputStore(fake_clock).publish(
            ReferenceInputs(
                costs=context.costs.model_copy(update={"data_origin": origin}),
                validation=validator().policy,
                ban_listed=False,
                news_halt=False,
                manually_blocked=False,
            ),
            actor="isolated-test-owner",
        )
    provider = fixture_provider()
    provider.data_origin = origin
    provider.get_quote.return_value = replace(provider.get_quote.return_value, data_origin=origin)
    if origin == DataOrigin.LIVE:
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            lower_circuit=Decimal("90"),
            upper_circuit=Decimal("110"),
        )
    worker = PaperWorker(
        executor,
        provider=provider,
        calendar=TradingCalendar(complete_years=[2026]),
        lock_path=tmp_path / "reference-worker.lock",
    )
    await worker.start(schedule=False)
    return worker, provider, client


@pytest.mark.parametrize(("costed", "news_disabled"), [(False, False), (True, False), (True, True)])
async def test_worker_produces_trade_exits_and_deduplicates_after_restart(
    db_engine, credentials, fake_clock, tmp_path, costed, news_disabled, monkeypatch
):
    monkeypatch.setenv("LLM_PROVIDER", "fallback")
    if news_disabled:
        monkeypatch.setenv("NEWS_ENABLED", "false")
        assert reload_settings().news_enabled is False
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=costed)
    try:
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == (111 if costed else 125)
            order = await session.scalar(sa.select(Order))
            proposal = await session.get(Proposal, order.proposal_id)
            assert proposal.origin == "QUANT"
            receipt = await session.get(LLMCall, proposal.llm_call_id)
            assert receipt.provider == "fallback"
            assert receipt.outcome == "FALLBACK_USED"
            assert receipt.cost_usd == 0
            assert receipt.input_tokens is None and receipt.output_tokens is None
            assert receipt.proposal_id == proposal.id
            assert TradeProposal.model_validate_json(receipt.raw_response).quantity == 1
            assert receipt.parsed_response["entry"] == "100"
            assert proposal.context_snapshot["llm_available"] is False
            assert await AuditService(fake_clock).verify(receipt.id)
            assert (
                proposal.context_snapshot["portfolio"]["source_id"]
                == "paper-account-reconciliation"
            )
        await worker.cycle()
        assert provider.get_candles.await_count == 2
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            ltp=Decimal(108),
            bids=(replace(provider.get_quote.return_value.bids[0], price=Decimal(108)),),
            asks=(replace(provider.get_quote.return_value.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.gross_pnl == (888 if costed else 1000)
            assert journal.charges == (Decimal("31.44") if costed else None)
            assert journal.net_pnl == (Decimal("856.56") if costed else None)
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
        await worker.stop()
        restored = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
        worker = PaperWorker(
            restored,
            provider=provider,
            calendar=worker.calendar,
            lock_path=tmp_path / "reference-worker.lock",
        )
        await worker.start(schedule=False)
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert provider.get_candles.await_count == 2
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert len(response.json()["orders"]) == 2
        assert len(response.json()["journal"]) == 1
        assert len(response.json()["advisory_calls"]) == 1
        assert response.json()["advisory_calls"][0]["proposal_id"] == proposal.id
        async with db_session.session_scope() as session:
            claims = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "REFERENCE_CYCLE_CLAIM"
                        )
                    )
                ).all()
            )
        assert len(claims) == 1
        assert await AuditService(fake_clock).verify(claims[0].chain_id)
    finally:
        await worker.stop()
        await client.aclose()


async def test_worker_missing_cost_evidence_stands_down_without_inventing_context(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, missing="inputs"
    )
    try:
        await worker.cycle()
        assert "EVIDENCE_UNAVAILABLE" in worker.detail
        assert not worker.failed
        provider.get_candles.assert_not_awaited()
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            assert (
                await session.scalar(
                    sa.select(AuditEvent).where(AuditEvent.event_type == "REFERENCE_STAND_DOWN")
                )
                is not None
            )
    finally:
        await worker.stop()
        await client.aclose()


async def test_interrupted_production_cannot_dispatch_approval_on_restart(
    db_engine, credentials, fake_clock, tmp_path, monkeypatch
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    try:
        monkeypatch.setattr(worker.reference_runtime, "_record", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await worker.cycle()
        await worker.stop()
        restored = PaperExecution(provider.get_quote, settings=worker.settings, clock=fake_clock)
        worker = PaperWorker(
            restored,
            provider=provider,
            calendar=worker.calendar,
            lock_path=tmp_path / "reference-worker.lock",
        )
        await worker.start(schedule=False)
        await worker.cycle()
        assert worker.failed
        assert "failed closed" in worker.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            assert (
                await session.scalar(
                    sa.select(Proposal).where(Proposal.strategy_id == "closed-candle-breakout")
                )
                is not None
            )
    finally:
        await worker.stop()
        await client.aclose()
