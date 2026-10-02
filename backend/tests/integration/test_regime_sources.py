"""Provider fixtures traverse actual regime storage and the reference worker."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.analysis.regime.classifier import RegimeDecision, evidence_fresh_at
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order
from app.execution.paper import PaperExecution
from app.marketdata.models import (
    Greeks,
    InstrumentRef,
    OHLCQuote,
    OptionChain,
    OptionLeg,
    OptionStrike,
)
from app.trading.inputs import ReferenceInputStore
from app.trading.regime_sources import ProviderRegimeSource
from app.trading.worker import PaperWorker
from tests.integration.test_contract_production import publish_policy
from tests.integration.test_reference_regime import configure_index
from tests.integration.test_reference_worker import credentials, setup_worker

__all__ = ["credentials"]


async def configure_sources(worker, provider, clock):
    publication, _, _ = await configure_index(worker, provider, clock)
    now = clock.now()
    identifiers = ("breadth-up", "breadth-down", "breadth-flat")
    async with db_session.session_scope() as session:
        for identifier in identifiers:
            session.add(
                Instrument(
                    id=identifier,
                    trading_symbol=identifier,
                    exchange=Exchange.NSE,
                    segment=Segment.CASH,
                    instrument_type=InstrumentType.EQUITY,
                )
            )
    quotes = {}
    for identifier, close in zip(identifiers, (101, 99, 100), strict=True):
        reference = InstrumentRef(identifier, Exchange.NSE, Segment.CASH)
        quotes[reference.key] = OHLCQuote(
            reference,
            Decimal(100),
            Decimal(101),
            Decimal(99),
            Decimal(close),
            now,
            previous_close=Decimal(100),
            data_origin=provider.data_origin,
        )
    provider.get_ohlc.return_value = quotes
    expiry = now.date() + timedelta(days=2)
    provider.get_option_chain.return_value = OptionChain(
        underlying="INDEX-FIXTURE",
        expiry=expiry,
        observed_at=now,
        spot=Decimal(1000),
        data_origin=provider.data_origin,
        strikes=(
            OptionStrike(
                Decimal(1000),
                OptionLeg(
                    "fixture-call",
                    OptionType.CE,
                    greeks=Greeks(
                        implied_volatility=Decimal(18),
                        computed_at=now,
                    ),
                ),
                OptionLeg(
                    "fixture-put",
                    OptionType.PE,
                    greeks=Greeks(
                        implied_volatility=Decimal(22),
                        computed_at=now,
                    ),
                ),
            ),
        ),
    )
    source = ProviderRegimeSource(
        source="isolated declared constituent fixture",
        known_at=now,
        valid_from=now,
        valid_until=now + timedelta(hours=1),
        breadth_instrument_ids=identifiers,
        option_underlying="INDEX-FIXTURE",
        option_expiry=expiry,
    )
    regime = publication.regime_source.model_copy(
        update={
            "implied_volatility": None,
            "breadth": None,
            "provider_source": source,
        }
    )
    clock.advance_seconds(1)
    await ReferenceInputStore(clock).publish(
        publication.model_copy(update={"regime_source": regime}),
        actor="fixture-owner",
    )
    return source


@pytest.mark.parametrize("restart", [False, True])
async def test_provider_breadth_and_iv_are_persisted_and_gate_actual_worker(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    restart,
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        source = await configure_sources(worker, provider, fake_clock)
        await publish_policy(worker, provider, fake_clock)
        quotes = provider.get_ohlc.return_value
        down = next(key for key in quotes if "down" in key)
        quotes[down] = replace(quotes[down], close=Decimal(101))
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            latest = await session.scalar(
                sa.select(RegimeHistory).order_by(RegimeHistory.ts.desc())
            )
            inputs = latest.decision["inputs"]
            assert Decimal(inputs["breadth"]["value"]) == Decimal(2) / 3
            assert Decimal(inputs["implied_volatility"]["value"]) == 20
            assert inputs["breadth"]["observed_at"] == source.known_at.isoformat()
            evidence = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.chain_id == inputs["breadth"]["source"],
                )
            )
            assert len(evidence.data_used["provider_observations"]["ohlc"]) == 3
            assert evidence.data_used["provider_observations"]["policy"] == source.model_dump(
                mode="json"
            )
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
        state = (await client.get("/api/v1/workspace")).json()
        assert state["regime"]["label"] == "TRENDING_UP"
        if restart:
            await worker.stop()
            executor = PaperExecution(
                provider.get_quote, settings=worker.settings, clock=fake_clock
            )
            worker = PaperWorker(
                executor,
                provider=provider,
                calendar=worker.calendar,
                lock_path=tmp_path / "reference-worker.lock",
            )
            await worker.start(schedule=False)
            provider.get_quote.return_value = replace(
                provider.get_quote.return_value,
                observed_at=fake_clock.now(),
            )
            await worker.cycle()
            assert not worker.failed, worker.detail
            assert len(await worker.executor.broker.list_orders()) == 1
        quote = provider.get_quote.return_value
        end = fake_clock.now().replace(second=0, microsecond=0)
        index_ref = InstrumentRef("INDEX-FIXTURE", Exchange.NSE, Segment.CASH)
        index_bars = await provider.get_candles(index_ref, 1, end - timedelta(minutes=29), end)
        equity_bars = await provider.get_candles(
            quote.instrument, 1, end - timedelta(minutes=21), end
        )
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        fake_clock.advance_seconds(120)
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(110),
            observed_at=fake_clock.now(),
            bids=(replace(quote.bids[0], price=Decimal("109.95")),),
            asks=(replace(quote.asks[0], price=Decimal(110)),),
        )
        equity_bars = (
            *equity_bars[2:],
            replace(
                equity_bars[-1],
                ts=equity_bars[-1].ts + timedelta(minutes=1),
                open=Decimal(100),
                low=Decimal(100),
                high=Decimal(108),
                close=Decimal(108),
            ),
            replace(
                equity_bars[-1],
                ts=equity_bars[-1].ts + timedelta(minutes=2),
                open=Decimal(108),
                low=Decimal(108),
                high=Decimal(110),
                close=Decimal(110),
            ),
        )
        index_bars = (
            *index_bars[2:],
            *(
                replace(
                    index_bars[-1],
                    ts=index_bars[-1].ts + timedelta(minutes=offset),
                    open=index_bars[-1].open + Decimal("0.05") * offset,
                    high=index_bars[-1].high + Decimal("0.05") * offset,
                    low=index_bars[-1].low + Decimal("0.05") * offset,
                    close=index_bars[-1].close + Decimal("0.05") * offset,
                )
                for offset in (1, 2)
            ),
        )

        async def next_candles(instrument, interval, start, finish):
            return index_bars if instrument == index_ref else equity_bars

        provider.get_candles.side_effect = next_candles
        provider.get_ohlc.return_value = {
            key: replace(value, observed_at=fake_clock.now()) for key, value in quotes.items()
        }
        chain = provider.get_option_chain.return_value
        row = chain.strikes[0]
        provider.get_option_chain.return_value = replace(
            chain,
            observed_at=fake_clock.now(),
            strikes=(
                replace(
                    row,
                    call=replace(
                        row.call, greeks=replace(row.call.greeks, computed_at=fake_clock.now())
                    ),
                    put=replace(
                        row.put, greeks=replace(row.put.greeks, computed_at=fake_clock.now())
                    ),
                ),
            ),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        assert worker.reference_runtime.detail == "RISK_APPROVED", worker.reference_runtime.detail
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 3
            latest = await session.scalar(
                sa.select(RegimeHistory).order_by(RegimeHistory.ts.desc())
            )
            evidence = await session.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.chain_id == latest.decision["inputs"]["breadth"]["source"],
                )
            )
            assert evidence.data_used["provider_observations"]["policy"] == source.model_dump(
                mode="json"
            )
            assert (
                latest.decision["inputs"]["breadth"]["observed_at"] == fake_clock.now().isoformat()
            )
            decision = RegimeDecision.model_validate(latest.decision)
            assert evidence_fresh_at(decision, fake_clock.now())
            expiry = fake_clock.now() + timedelta(seconds=1)
            decision = decision.model_copy(
                update={
                    "inputs": decision.inputs.model_copy(update={"valid_until": expiry}),
                }
            )
            assert not evidence_fresh_at(decision, expiry)
    finally:
        await worker.stop()
        await client.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_constituent",
        "stale_breadth",
        "future_breadth",
        "wrong_origin",
        "stale_greeks",
        "missing_iv",
    ],
)
async def test_provider_failures_never_create_orders(
    db_engine,
    credentials,
    fake_clock,
    tmp_path,
    failure,
):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await configure_sources(worker, provider, fake_clock)
        quotes = provider.get_ohlc.return_value
        key = next(iter(quotes))
        if failure == "missing_constituent":
            del quotes[key]
        elif failure in ("stale_breadth", "future_breadth"):
            quotes[key] = replace(
                quotes[key],
                observed_at=fake_clock.now()
                + timedelta(
                    seconds=-120 if failure == "stale_breadth" else 1,
                ),
            )
        else:
            chain = provider.get_option_chain.return_value
            if failure == "wrong_origin":
                chain = replace(chain, data_origin=DataOrigin.LIVE)
            else:
                row = chain.strikes[0]
                greeks = replace(
                    row.call.greeks,
                    implied_volatility=None if failure == "missing_iv" else Decimal(18),
                    computed_at=fake_clock.now() - timedelta(seconds=120),
                )
                chain = replace(
                    chain, strikes=(replace(row, call=replace(row.call, greeks=greeks)),)
                )
            provider.get_option_chain.return_value = chain
        await worker.cycle()
        assert not await worker.executor.broker.list_orders()
        if failure == "missing_iv":
            assert not worker.failed, worker.detail
            state = (await client.get("/api/v1/workspace")).json()
            assert state["regime"]["label"] == "UNKNOWN"
        else:
            assert worker.failed and "REGIME_REFRESH_FAILED" in worker.detail
    finally:
        await worker.stop()
        await client.aclose()
