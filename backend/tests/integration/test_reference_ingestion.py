"""Only the external provider is mocked; ingestion, storage, analysis and gates are real."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import create_autospec

import pytest
import sqlalchemy as sa

from app.analysis.events import EventBlackout
from app.analysis.regime.classifier import classify
from app.audit.service import AuditService
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.market_data import Candle
from app.marketdata.base import MarketDataProvider
from app.marketdata.models import Bar, DepthLevel, InstrumentRef, Quote
from app.modes import TradingMode
from app.strategies.context import ContextValue
from app.strategies.engine import StrategyEngine
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry
from app.trading.observations import ReferenceIngestion
from tests.integration.test_fundamentals import seed
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_regime import inputs, policy


def fixture_provider():
    provider = create_autospec(MarketDataProvider, instance=True)
    provider.name = "isolated-deterministic-fixture"
    provider.data_origin = DataOrigin.SYNTHETIC
    bars = tuple(
        Bar(
            OBSERVED - timedelta(minutes=21 - index),
            Decimal(98),
            Decimal(99),
            Decimal(97),
            Decimal(98),
            100,
        )
        for index in range(21)
    )
    bars = (*bars[:-1], replace(bars[-1], high=Decimal(100), low=Decimal(98), close=Decimal(100)))
    provider.get_candles.return_value = bars
    provider.get_quote.return_value = Quote(
        InstrumentRef("TEST", Exchange.NSE, Segment.CASH),
        Decimal(100),
        OBSERVED,
        DataOrigin.SYNTHETIC,
        bids=(DepthLevel(Decimal("99.95"), 1000),),
        asks=(DepthLevel(Decimal(100), 1000),),
    )
    return provider


async def test_provider_to_real_breakout_and_persisted_enablement(db_engine, fake_clock):
    await seed()
    fake_clock.set_to(OBSERVED)
    provider = fixture_provider()
    observation = await ReferenceIngestion(provider, clock=fake_clock).collect("ins-test")
    assert observation.available["indicators"].value == {
        "sma20": Decimal("98.1"),
        "atr14": Decimal(2),
    }
    assert await AuditService(fake_clock).verify(observation.chain_id)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Candle)) == 21
    regime = classify(inputs(), policy())
    available = observation.available | {
        "regime": ContextValue(
            observed_at=OBSERVED, available_at=OBSERVED, origin=DataOrigin.SYNTHETIC, value=regime
        )
    }
    strategy = ClosedCandleBreakout("ins-test", "NSE:TEST", "0.05", "0.005", clock=fake_clock)
    registry = StrategyRegistry()
    await registry.register(strategy, enabled_paper=True)
    engine = StrategyEngine(registry, fake_clock)
    arguments = {
        "instrument_id": "ins-test",
        "instrument_key": "NSE:TEST",
        "as_of": OBSERVED,
        "regime_id": "isolated-regime",
        "regime": regime,
        "available": available,
        "llm_available": False,
        "blackout": EventBlackout(
            symbol="TEST", as_of=OBSERVED, status="CLEAR", event_ids=(), restricted_strategies=()
        ),
    }
    evaluated = await engine.evaluate(strategy, **arguments)
    assert evaluated.reason == "SIGNAL"
    assert (evaluated.signal.entry, evaluated.signal.stop, evaluated.signal.targets) == (
        Decimal(100),
        Decimal(96),
        (Decimal(108),),
    )
    assert "quantity" not in evaluated.signal.model_dump()
    await registry.set_enabled(strategy.spec.id, "1", TradingMode.PAPER, False)
    assert (await engine.evaluate(strategy, **arguments)).reason == "STRATEGY_MODE_DISABLED"


@pytest.mark.parametrize("invalid", ["stale", "future", "forming", "gap", "origin", "zero"])
async def test_invalid_provider_input_is_audited_without_persisted_bars(
    db_engine, fake_clock, invalid
):
    await seed()
    fake_clock.set_to(OBSERVED)
    provider = fixture_provider()
    quote = provider.get_quote.return_value
    bars = provider.get_candles.return_value
    if invalid in {"stale", "future"}:
        quote = replace(
            quote, observed_at=OBSERVED + timedelta(seconds=-61 if invalid == "stale" else 1)
        )
    elif invalid == "origin":
        quote = replace(quote, data_origin=DataOrigin.LIVE)
    elif invalid == "zero":
        quote = replace(quote, ltp=Decimal(0))
    elif invalid == "forming":
        bars = (*bars[:-1], replace(bars[-1], ts=OBSERVED))
    else:
        bars = (replace(bars[0], ts=bars[0].ts - timedelta(minutes=1)), *bars[1:])
    provider.get_quote.return_value = quote
    provider.get_candles.return_value = bars
    with pytest.raises(ValueError):
        await ReferenceIngestion(provider, clock=fake_clock).collect("ins-test")
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Candle)) == 0
        event = await session.scalar(sa.select(AuditEvent))
        assert event.event_type == "MARKET_INPUT_REJECTED"
        assert await AuditService(fake_clock).verify(event.chain_id)
