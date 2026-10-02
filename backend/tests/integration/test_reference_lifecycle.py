"""Provider fixture through real reference analysis, shared risk and PAPER execution."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.analysis.events import EventBlackout
from app.analysis.regime.events import EventCalendar
from app.audit.service import AuditService
from app.core.calendar import TradingCalendar
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Position
from app.modes import TradingMode
from app.risk.safety import RiskSafety
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry
from app.trading.observations import ReferenceIngestion
from app.trading.reference import ReferenceDecisionService
from tests.integration.test_auth import credentials
from tests.integration.test_paper_execution import setup_execution
from tests.integration.test_proposal import validator
from tests.integration.test_reference_ingestion import fixture_provider

__all__ = ["credentials"]


@pytest.mark.parametrize("blocker", [None, "disabled", "risk", "calendar"])
async def test_reference_signal_reaches_paper_exit_journal_and_api(
    db_engine, credentials, fake_clock, blocker
):
    executor, _unused_proposal, _market, client, context = await setup_execution(
        credentials, fake_clock
    )
    try:
        if blocker != "calendar":
            now = fake_clock.now()
            calendar = EventCalendar(
                source="isolated-fixture-calendar",
                known_at=now,
                coverage_start=now - timedelta(hours=1),
                coverage_end=now + timedelta(hours=1),
                events=(),
            )
            async with db_session.session_scope() as session:
                row = await session.get(RegimeHistory, context.regime_id)
                row.decision = {
                    **row.decision,
                    "inputs": {
                        **row.decision["inputs"],
                        "calendar": calendar.model_dump(mode="json"),
                    },
                }
        strategy = ClosedCandleBreakout("ins-test", "NSE:TEST", "0.05", "0.005", clock=fake_clock)
        await StrategyRegistry().register(strategy, enabled_paper=True)
        context = context.model_copy(
            update={
                "strategy": strategy.spec,
                "portfolio": context.portfolio.model_copy(update={"strategy_id": strategy.spec.id}),
                "blackout": EventBlackout(
                    symbol="TEST",
                    as_of=fake_clock.now(),
                    status="CLEAR",
                    event_ids=(),
                    restricted_strategies=(),
                ),
            }
        )
        provider = fixture_provider()
        executor.source = provider.get_quote
        service = ReferenceDecisionService(
            ReferenceIngestion(provider, clock=fake_clock),
            validator(),
            calendar=TradingCalendar(complete_years=[2026]),
        )
        if blocker == "disabled":
            await StrategyRegistry().set_enabled(strategy.spec.id, "1", TradingMode.PAPER, False)
        elif blocker == "risk":
            await RiskSafety().trip_error("PAPER", "SYNTHETIC")
        result = await service.evaluate(strategy, context)
        if blocker in {"disabled", "risk"}:
            assert result.approved_quantity == 0
            assert result.code == (
                "STRATEGY_MODE_DISABLED" if blocker == "disabled" else "RISK_ERROR_LATCHED"
            )
            assert await executor.broker.list_orders() == []
            assert await AuditService().verify(result.candidate_id)
            return
        assert result.code == "RISK_APPROVED"
        assert result.approved_quantity == 125
        if blocker == "calendar":
            with pytest.raises(SafetyError, match="EVENT_BLACKOUT_OR_UNAVAILABLE"):
                await executor.submit(result.proposal_id)
            assert await executor.broker.list_orders() == []
            return
        order_id = await executor.submit(result.proposal_id)
        assert await executor.submit(result.proposal_id) == order_id
        await executor.verify_protection()
        async with db_session.session_scope() as session:
            position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 125 and position.stop_loss_price == 96
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert len(response.json()["orders"]) == 1
        provider.get_quote.return_value = replace(
            provider.get_quote.return_value,
            ltp=Decimal(108),
            bids=(replace(provider.get_quote.return_value.bids[0], price=Decimal(108)),),
            asks=(replace(provider.get_quote.return_value.asks[0], price=Decimal("108.05")),),
        )
        await executor.monitor_once()
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            assert journal.strategy_id == strategy.spec.id
            assert journal.gross_pnl == Decimal(1000)
            assert journal.net_pnl is None
        assert await AuditService().verify(result.proposal_id)
        assert await AuditService().verify(result.candidate_id)
        response = await client.get("/api/v1/workspace")
        assert len(response.json()["orders"]) == 2
        assert len(response.json()["journal"]) == 1
    finally:
        await client.aclose()
