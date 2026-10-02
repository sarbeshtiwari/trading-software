"""Index fixtures enter the real worker; no precomputed regime is substituted."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.analysis.regime.events import EventCalendar
from app.analysis.regime.inputs import IndicatorPolicy
from app.core.enums import Exchange, InstrumentType, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.journal import JournalEntry
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order
from app.marketdata.models import Bar
from app.trading.inputs import ReferenceInputStore
from app.trading.regime import RegimeSource
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.unit.test_regime import observation, policy

__all__ = ["credentials"]


async def configure_index(worker, provider, fake_clock, *, missing=False):
    async with db_session.session_scope() as session:
        session.add(
            Instrument(
                id="index-fixture",
                trading_symbol="INDEX-FIXTURE",
                exchange=Exchange.NSE,
                segment=Segment.CASH,
                instrument_type=InstrumentType.INDEX,
            )
        )
    existing = await ReferenceInputStore(fake_clock).at(
        "ins-test", provider.data_origin, fake_clock.now()
    )
    async with db_session.session_scope() as session:
        regime = await session.get(RegimeHistory, "reg-test")
        calendar = EventCalendar.model_validate(regime.decision["inputs"]["calendar"])
    source = RegimeSource(
        index_instrument_id="index-fixture",
        indicators=IndicatorPolicy(
            adx_period=14,
            fast_period=5,
            slow_period=20,
            volatility_period=14,
            periods_per_year=252 * 375,
            bar_seconds=60,
        ),
        policy=policy(),
        implied_volatility=observation(20),
        breadth=None
        if missing is True
        else observation("0.6", at=fake_clock.now() - timedelta(seconds=120))
        if missing == "stale"
        else observation("0.6"),
        calendar=calendar,
    )
    fake_clock.advance_seconds(1)
    publication = existing.model_copy(update={"regime_source": source})
    response = await ReferenceInputStore(fake_clock).publish(publication, actor="fixture-owner")
    equity = provider.get_candles.return_value
    end = fake_clock.now().replace(second=0, microsecond=0)
    bars = tuple(
        Bar(
            end - timedelta(minutes=29 - index),
            Decimal(1000) + Decimal("0.05") * index,
            Decimal(1001) + Decimal("0.05") * index,
            Decimal(999) + Decimal("0.05") * index,
            Decimal(1000) + Decimal("0.05") * index,
            1000,
        )
        for index in range(29)
    )

    async def candles(instrument, interval, start, finish):
        return bars if instrument.trading_symbol == "INDEX-FIXTURE" else equity

    provider.get_candles.side_effect = candles
    return publication, response, bars


@pytest.mark.parametrize("missing", [False, True, "stale"])
async def test_index_to_regime_to_worker_decision_and_journal(
    db_engine, credentials, fake_clock, tmp_path, missing
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await configure_index(worker, provider, fake_clock, missing=missing)
        await worker.cycle()
        assert not worker.failed, worker.detail
        state = (await client.get("/api/v1/workspace")).json()
        assert state["regime"]["label"] == ("UNKNOWN" if missing else "TRENDING_UP")
        assert state["regime_status"] == ("STALE_OR_INCOMPLETE" if missing else "RECORDED_SNAPSHOT")
        async with db_session.session_scope() as session:
            latest = await session.scalar(
                sa.select(RegimeHistory).order_by(RegimeHistory.ts.desc())
            )
            assert latest.id != "reg-test"
            assert latest.decision["label"] == ("UNKNOWN" if missing else "TRENDING_UP")
            source_id = latest.decision["inputs"]["adx"]["source"]
            evidence = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.chain_id == source_id)
            )
            assert evidence.instrument_id == "index-fixture"
            assert len(evidence.data_used["candles"]) == 29
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == (
                0 if missing else 1
            )
        if not missing:
            quote = provider.get_quote.return_value
            provider.get_quote.return_value = replace(
                quote,
                ltp=Decimal(108),
                bids=(replace(quote.bids[0], price=Decimal(108)),),
                asks=(replace(quote.asks[0], price=Decimal("108.05")),),
            )
            await worker.cycle()
            assert not worker.failed, worker.detail
            async with db_session.session_scope() as session:
                journal = await session.scalar(sa.select(JournalEntry))
                assert journal.net_pnl == Decimal("856.56")
                assert journal.regime_at_entry.value == "TRENDING_UP"
                assert journal.indicator_snapshot["decision_context"]["regime_id"] == latest.id
            state = (await client.get("/api/v1/workspace")).json()
            assert state["journal"][0]["regime_at_entry"] == "TRENDING_UP"
            assert state["journal"][0]["regime_id"] == latest.id
    finally:
        await worker.stop()
        await client.aclose()


async def test_future_index_candle_stands_down_without_order(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        _, _, bars = await configure_index(worker, provider, fake_clock)
        provider.get_candles.side_effect = None
        provider.get_candles.return_value = (
            *bars[:-1],
            replace(bars[-1], ts=fake_clock.now() + timedelta(minutes=1)),
        )
        await worker.cycle()
        assert worker.failed and "REGIME_REFRESH_FAILED" in worker.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            rejection = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "REFERENCE_STAND_DOWN")
            )
            assert rejection.result["reason"] == "REGIME_REFRESH_FAILED"
    finally:
        await worker.stop()
        await client.aclose()


async def test_owner_cannot_publish_future_regime_source(
    db_engine, credentials, fake_clock, tmp_path
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path)
    try:
        publication, _, _ = await configure_index(worker, provider, fake_clock)
        body = publication.model_dump(mode="json")
        future = fake_clock.now() + timedelta(minutes=1)
        body["regime_source"]["breadth"] = observation("0.6", at=future).model_dump(mode="json")
        response = await client.post(
            "/api/v1/strategies/reference/inputs",
            json={"inputs": body, "reason": "Isolated future-source rejection fixture"},
        )
        assert response.status_code == 422
        body["regime_source"]["breadth"] = observation("0.6").model_dump(mode="json")
        response = await client.post(
            "/api/v1/strategies/reference/inputs",
            json={"inputs": body, "reason": "Isolated known-source publication fixture"},
        )
        assert response.status_code == 200, response.text
        await worker.cycle()
        assert not worker.failed, worker.detail
    finally:
        await worker.stop()
        await client.aclose()
