"""Synthetic provider boundary; real snapshot and audit persistence."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.data_origin import DataOrigin
from app.core.enums import GreekSource, InstrumentType, Segment
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal, RiskDecision
from app.db.models.instrument import Instrument
from app.db.models.journal import JournalEntry
from app.db.models.regime import RegimeHistory
from app.db.models.trading import Order, Position
from app.execution.paper import PaperExecution
from app.fno.chain.snapshots import ChainSnapshotStore
from app.marketdata.models import Greeks, OptionChain, OptionLeg, OptionStrike
from app.portfolio.cost_store import CostStore
from app.trading.contracts import LongOptionContractSource
from app.trading.inputs import ReferenceInputStore
from app.trading.options import OptionEvidenceSource, require_option_observation
from app.trading.worker import PaperWorker
from tests.integration.test_fno_bans import admission
from tests.integration.test_paper_option_account import option_contract
from tests.integration.test_pipeline import setup_context, validator
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.unit.test_costs import schedule

__all__ = ["credentials"]


class FixtureProvider:
    data_origin = DataOrigin.SYNTHETIC

    def __init__(self, chain):
        self.chain = chain
        self.requests = []

    async def get_option_chain(self, underlying, expiry):
        self.requests.append((underlying, expiry))
        return self.chain


async def fixture(clock):
    await option_contract(clock)
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "fixture-contract")
    greeks = Greeks(
        delta=Decimal(".5"),
        gamma=Decimal(".01"),
        theta=Decimal("-.1"),
        vega=Decimal(".2"),
        rho=Decimal(".01"),
        implied_volatility=Decimal(20),
        source=GreekSource.BROKER,
        computed_at=clock.now(),
    )
    leg = OptionLeg(
        instrument.trading_symbol,
        instrument.option_type,
        ltp=Decimal(100),
        bid=Decimal(99),
        ask=Decimal(100),
        greeks=greeks,
    )
    chain = OptionChain(
        instrument.underlying,
        instrument.expiry_date,
        clock.now(),
        strikes=(OptionStrike(instrument.strike_price, call=leg),),
        data_origin=DataOrigin.SYNTHETIC,
    )
    return instrument, chain


async def test_option_observation_is_reconstructable_after_restart(db_engine, fake_clock):
    instrument, chain = await fixture(fake_clock)
    provider = FixtureProvider(chain)
    evidence = await OptionEvidenceSource(provider, clock=fake_clock).observe(
        instrument,
        origin=DataOrigin.SYNTHETIC,
        max_age_seconds=30,
    )
    assert provider.requests == [(instrument.underlying, instrument.expiry_date)]
    assert evidence.observed_at == chain.observed_at
    assert (
        await require_option_observation(
            instrument,
            evidence,
            as_of=fake_clock.now(),
            origin=DataOrigin.SYNTHETIC,
            max_age_seconds=30,
        )
        == chain
    )
    async with db_session.session_scope() as session:
        audit = await session.scalar(
            sa.select(AuditEvent).where(AuditEvent.chain_id == evidence.source_id)
        )
    assert audit.instrument_id == instrument.id
    assert audit.data_used["contract"]["symbol"] == "FIXTURE-CE"
    assert audit.data_used["greeks"]["delta"] == "0.5"
    assert await AuditService(fake_clock).verify(evidence.source_id, expected_count=1)
    restored = ChainSnapshotStore(cadence=timedelta(seconds=1), clock=fake_clock)
    assert await restored.read(
        instrument.underlying, instrument.expiry_date, as_of=fake_clock.now()
    ) == [chain]
    assert (
        await restored.read(
            instrument.underlying,
            instrument.expiry_date,
            as_of=fake_clock.now() - timedelta(microseconds=1),
        )
        == []
    )


@pytest.mark.parametrize(
    "defect", ["missing", "forged", "retimestamp", "lot", "type", "stale", "future"]
)
async def test_source_revalidation_refuses_forgery_and_catalog_drift(db_engine, fake_clock, defect):
    instrument, chain = await fixture(fake_clock)
    evidence = await OptionEvidenceSource(FixtureProvider(chain), clock=fake_clock).observe(
        instrument,
        origin=DataOrigin.SYNTHETIC,
        max_age_seconds=30,
    )
    if defect == "missing":
        evidence = None
    elif defect == "forged":
        evidence = evidence.model_copy(update={"source_id": "not-an-observation"})
    elif defect == "retimestamp":
        evidence = evidence.model_copy(
            update={"observed_at": evidence.observed_at - timedelta(seconds=1)}
        )
    elif defect == "lot":
        instrument.lot_size += 1
    elif defect == "type":
        instrument.instrument_type = InstrumentType.FUTURE
    elif defect == "stale":
        fake_clock.advance(timedelta(seconds=31))
    elif defect == "future":
        fake_clock.advance(-timedelta(seconds=1))
    with pytest.raises(ValueError):
        await require_option_observation(
            instrument,
            evidence,
            as_of=fake_clock.now(),
            origin=DataOrigin.SYNTHETIC,
            max_age_seconds=30,
        )


async def test_shared_decision_gate_requires_the_persisted_option_source(db_engine, fake_clock):
    context = await setup_context()
    fake_clock.set_to(context.market.as_of)
    template, chain = await fixture(fake_clock)
    leg = replace(chain.strikes[0].call, trading_symbol="TEST")
    chain = replace(chain, underlying="TEST", strikes=(replace(chain.strikes[0], call=leg),))
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, "ins-test")
        instrument.instrument_type = InstrumentType.OPTION
        instrument.segment = Segment.FNO
        instrument.expiry_date = template.expiry_date
        instrument.option_type = template.option_type
        instrument.strike_price = template.strike_price
        instrument.underlying = "TEST"
        regime = await session.get(RegimeHistory, context.regime_id)
    evidence = await OptionEvidenceSource(FixtureProvider(chain), clock=fake_clock).observe(
        instrument,
        origin=DataOrigin.SYNTHETIC,
        max_age_seconds=30,
    )
    context = context.model_copy(
        update={"market": context.market.model_copy(update={"greeks": evidence})}
    )
    pipeline = DecisionPipeline(validator())
    assert await pipeline._gate(context, instrument, regime, "QUANT") is None
    forged = evidence.model_copy(update={"source_id": "invented-option-greeks"})
    context = context.model_copy(
        update={"market": context.market.model_copy(update={"greeks": forged})}
    )
    assert (
        await pipeline._gate(context, instrument, regime, "QUANT")
        == "OPTION_OBSERVATION_UNAVAILABLE_OR_CHANGED"
    )


async def setup_option_worker(
    credentials, fake_clock, tmp_path, *, missing=False, minimum_dte=2, fill_config=None
):
    worker, provider, client = await setup_worker(
        credentials, fake_clock, tmp_path, fill_config=fill_config
    )
    try:
        _, chain = await fixture(fake_clock)
        leg = replace(chain.strikes[0].call, trading_symbol="TEST")
        if missing:
            leg = replace(leg, greeks=None)
        chain = replace(chain, underlying="TEST", strikes=(replace(chain.strikes[0], call=leg),))
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, "ins-test")
            instrument.segment = Segment.FNO
            instrument.instrument_type = InstrumentType.OPTION
            instrument.underlying = "TEST"
            instrument.expiry_date = chain.expiry
            instrument.strike_price = chain.strikes[0].strike
            instrument.option_type = leg.option_type
        response = await client.post(
            "/api/v1/strategies/reference/register",
            json={
                "instrument_id": "ins-test",
                "kind": "LONG_OPTION",
                "reason": "Register isolated option hypothesis",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["strategy_id"] == "long-option-breakout"
        for strategy_id, enabled in (
            ("closed-candle-breakout", False),
            ("long-option-breakout", True),
        ):
            response = await client.post(
                f"/api/v1/strategies/{strategy_id}/paper",
                json={
                    "version": "1",
                    "enabled": enabled,
                    "reason": "Use isolated option strategy",
                },
            )
            assert response.status_code == 200, response.text
        response = await client.put("/api/v1/risk/fno-bans", json=admission(fake_clock, symbols=()))
        assert response.status_code == 200, response.text
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote, instrument=replace(quote.instrument, segment=Segment.FNO)
        )
        provider.get_option_chain.return_value = chain
        store = ReferenceInputStore(fake_clock)
        previous = await store.at("ins-test", provider.data_origin, fake_clock.now())
        fake_clock.advance(timedelta(seconds=1))
        now = fake_clock.now()
        source = LongOptionContractSource(
            instrument_id="ins-test",
            data_origin=provider.data_origin,
            source="synthetic owner long-option policy",
            known_at=now,
            valid_from=now,
            valid_until=now + timedelta(minutes=5),
            risk_cost_reserve_per_unit=Decimal(".5"),
            kind="LONG_OPTION",
            minimum_days_to_expiry=minimum_dte,
        )
        await store.publish(
            previous.model_copy(update={"costs": None, "contract_source": source}),
            actor="test-owner",
        )
        restored = await store.at("ins-test", provider.data_origin, now)
        assert isinstance(restored.contract_source, LongOptionContractSource)
        await CostStore(fake_clock).publish(
            schedule(
                segment=Segment.FNO,
                charge_basis="OPTION_PREMIUM",
                known_at=now,
                effective_from=now,
                effective_to=now + timedelta(hours=1),
            ),
            actor="test-owner",
            reason="Synthetic option tariff",
        )
        return worker, provider, client, chain
    except BaseException:
        await worker.stop()
        await client.aclose()
        raise


@pytest.mark.parametrize(
    ("missing", "minimum_dte", "pending"),
    [
        (False, 2, False),
        (True, 2, False),
        (False, 8, False),
        (False, 2, True),
    ],
)
async def test_real_worker_option_lifecycle_and_stand_down(
    db_engine, credentials, fake_clock, tmp_path, missing, minimum_dte, pending
):
    worker, provider, client, chain = await setup_option_worker(
        credentials, fake_clock, tmp_path, missing=missing, minimum_dte=minimum_dte
    )
    try:
        if pending:
            quote = provider.get_quote.return_value
            provider.get_quote.return_value = replace(
                quote, asks=(replace(quote.asks[0], price=Decimal(101)),)
            )
        await worker.cycle()
        assert not worker.failed, worker.detail
        provider.get_option_chain.assert_awaited_once_with("TEST", chain.expiry)
        async with db_session.session_scope() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == (
                1 if not missing and minimum_dte == 2 else 0
            )
            observations = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "PAPER_OPTION_OBSERVATION"
                        )
                    )
                ).all()
            )
            assert len(observations) == (0 if missing else 1)
            contracts = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.event_type == "PAPER_CONTRACT_OBSERVATION"
                        )
                    )
                ).all()
            )
            if not missing and minimum_dte == 2:
                assert len(contracts) == 1
                assert Decimal(contracts[0].result["defined_max_loss_per_unit"]) == (
                    101 if pending else 100
                )
                assert Decimal(contracts[0].result["margin_per_unit"]) == (101 if pending else 100)
                assert (
                    contracts[0].data_used["option_evidence"]["source_id"]
                    == observations[0].chain_id
                )
                proposal = await session.scalar(
                    sa.select(Proposal).where(Proposal.strategy_id == "long-option-breakout")
                )
                assert proposal is not None
                assert proposal.status == "RISK_APPROVED"
                assert proposal.approved_quantity == 4
                assert proposal.target_price == 400
                risk = await session.scalar(
                    sa.select(RiskDecision).where(
                        RiskDecision.proposal_id == proposal.id,
                        RiskDecision.is_preflight.is_(False),
                    )
                )
                assert risk.approved and risk.approved_quantity == 4
            else:
                assert contracts == []
            if missing:
                events = list(
                    (
                        await session.scalars(
                            sa.select(AuditEvent).where(
                                AuditEvent.event_type == "REFERENCE_STAND_DOWN"
                            )
                        )
                    ).all()
                )
                assert any(
                    event.result.get("reason") == "OPTION_EVIDENCE_UNAVAILABLE" for event in events
                )
        if not missing and minimum_dte == 2:
            worker = await verify_option_lifecycle(
                worker, provider, client, fake_clock, tmp_path, proposal.id, pending=pending
            )
    finally:
        await worker.stop()
        await client.aclose()


async def verify_option_lifecycle(
    worker, provider, client, clock, tmp_path, proposal_id, *, pending=False
):
    async with db_session.session_scope() as session:
        position = await session.scalar(sa.select(Position))
        order = await session.scalar(sa.select(Order))
    if pending:
        assert position is None
        assert worker.executor.broker.account.used_margin == 0
        committed = await worker.executor.portfolio_state(
            "long-option-breakout", DataOrigin.SYNTHETIC
        )
        assert committed.open_and_pending_positions == 1
        assert committed.reserved_risk == Decimal("416.28")
        assert sum(item.notional for item in committed.exposures) == Decimal(404)
        assert committed.available_margin == (
            worker.executor.broker.account.available_margin - Decimal("416.28")
        )
    else:
        assert position.net_quantity == 4
        assert position.stop_loss_price == 96 and position.target_price == 400
        assert worker.executor.broker.account.used_margin == 400
        account = await worker.executor.portfolio_state(
            "long-option-breakout", DataOrigin.SYNTHETIC
        )
        assert account.reserved_risk == Decimal("412.28")
    assert await worker.executor.submit(proposal_id) == order.id
    await worker.executor.verify_protection()
    await worker.stop()
    executor = PaperExecution(provider.get_quote, settings=worker.settings, clock=clock)
    restored = PaperWorker(
        executor,
        provider=provider,
        calendar=worker.calendar,
        lock_path=tmp_path / "reference-worker.lock",
    )
    await restored.start(schedule=False)
    try:
        if pending:
            clock.advance(timedelta(seconds=1))
            quote = provider.get_quote.return_value
            provider.get_quote.return_value = replace(
                quote, observed_at=clock.now(), asks=(replace(quote.asks[0], price=Decimal(100)),)
            )
            await restored.cycle()
            assert not restored.failed, restored.detail
            async with db_session.session_scope() as session:
                position = await session.scalar(sa.select(Position))
            assert position.net_quantity == 4
        assert executor.broker.account.used_margin == 400
        await executor.verify_protection()
        clock.advance(timedelta(seconds=1))
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            observed_at=clock.now(),
            ltp=Decimal(96),
            bids=(replace(quote.bids[0], price=Decimal(96)),),
            asks=(replace(quote.asks[0], price=Decimal("96.05")),),
        )
        await restored.cycle()
        assert not restored.failed, restored.detail
        async with db_session.session_scope() as session:
            journal = await session.scalar(sa.select(JournalEntry))
            closed = await session.get(Position, position.id)
            assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
        assert closed.net_quantity == 0
        assert journal.gross_pnl == -16
        assert journal.charges == Decimal("11.93")
        assert journal.net_pnl == Decimal("-27.93")
        assert executor.broker.account.cash == Decimal("99972.07")
        assert executor.broker.account.used_margin == 0
        response = await client.get("/api/v1/workspace")
        assert response.status_code == 200
        assert len(response.json()["orders"]) == 2
        assert Decimal(response.json()["journal"][0]["net_pnl"]) == Decimal("-27.93")
        assert await AuditService(clock).verify(proposal_id)
        assert await AuditService(clock).verify(order.id)
    except BaseException:
        await restored.stop()
        raise
    return restored


@pytest.mark.parametrize(
    "defect", ["stale", "future", "origin", "symbol", "expiry", "missing", "computed", "delta"]
)
async def test_option_source_refuses_invented_or_mismatched_evidence(db_engine, fake_clock, defect):
    instrument, chain = await fixture(fake_clock)
    leg = chain.strikes[0].call
    if defect == "stale":
        fake_clock.advance(timedelta(seconds=31))
    elif defect == "future":
        chain = replace(chain, observed_at=fake_clock.now() + timedelta(seconds=1))
    elif defect == "origin":
        chain = replace(chain, data_origin=DataOrigin.LIVE)
    elif defect == "expiry":
        chain = replace(chain, expiry=chain.expiry + timedelta(days=1))
    else:
        if defect == "symbol":
            leg = replace(leg, trading_symbol="ANOTHER-CONTRACT")
        elif defect == "missing":
            leg = replace(leg, greeks=None)
        elif defect == "computed":
            leg = replace(leg, greeks=replace(leg.greeks, source=GreekSource.COMPUTED))
        elif defect == "delta":
            leg = replace(leg, greeks=replace(leg.greeks, delta=Decimal(2)))
        chain = replace(chain, strikes=(replace(chain.strikes[0], call=leg),))
    with pytest.raises(ValueError):
        await OptionEvidenceSource(FixtureProvider(chain), clock=fake_clock).observe(
            instrument,
            origin=DataOrigin.SYNTHETIC,
            max_age_seconds=30,
        )
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 0
