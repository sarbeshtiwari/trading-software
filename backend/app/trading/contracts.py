"""Fresh PAPER CASH observations from quotes and explicit effective cost inputs."""

from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.agents.pipeline import ContractCosts
from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.brokers.models import OrderRequest
from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import InstrumentType, OrderType, Product, Segment, TransactionType
from app.core.ids import new_id
from app.fno.expiry import select_expiry
from app.fno.options import option_contract
from app.marketdata.models import InstrumentRef
from app.modes import TradingMode
from app.portfolio.cost_store import CostStore
from app.trading.options import require_option_observation


class CashContractSource(EvidenceModel):
    instrument_id: str = Field(min_length=1)
    data_origin: DataOrigin
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    risk_cost_reserve_per_unit: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def valid_window(self):
        if self.known_at > self.valid_from or self.valid_from >= self.valid_until:
            raise ValueError("invalid contract/restriction policy window")
        if self.valid_until - self.valid_from > timedelta(days=1):
            raise ValueError("PAPER intraday policy cannot exceed one day")
        return self

    def valid_at(self, as_of):
        return self.known_at <= self.valid_from <= as_of < self.valid_until


class LongOptionContractSource(CashContractSource):
    kind: Literal["LONG_OPTION"]
    minimum_days_to_expiry: int = Field(ge=0, strict=True)


async def derive_contract_costs(
    executor, instrument, observation, inputs, *, max_age_seconds, option_evidence=None
):
    source = inputs.contract_source
    now = executor.clock.now()
    quote = observation.quote
    long_option = isinstance(source, LongOptionContractSource)
    expected = (
        (Segment.FNO, InstrumentType.OPTION)
        if long_option
        else (Segment.CASH, InstrumentType.EQUITY)
    )
    if (
        executor.settings.trading_mode != TradingMode.PAPER
        or executor.broker.execution_realism != ExecutionRealism.SIMULATED
        or (instrument.segment, instrument.instrument_type) != expected
        or not instrument.is_active
        or instrument.id != source.instrument_id
        or quote.instrument
        != InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
        or not inputs.publication_id
        or not source.valid_at(now)
        or quote.data_origin != source.data_origin
        or not timedelta(0) <= now - quote.observed_at <= timedelta(seconds=max_age_seconds)
        or quote.best_ask is None
        or quote.best_bid is None
        or quote.is_crossed
        or instrument.lot_size <= 0
    ):
        raise ValueError("PAPER contract inputs unavailable or inconsistent")
    if long_option:
        if (
            select_expiry(
                [option_contract(instrument)], as_of=now, min_days=source.minimum_days_to_expiry
            )
            != instrument.expiry_date
        ):
            raise ValueError("option minimum days to expiry not satisfied")
        await require_option_observation(
            instrument,
            option_evidence,
            as_of=now,
            origin=source.data_origin,
            max_age_seconds=max_age_seconds,
        )
    price = max(quote.ltp, quote.best_ask, quote.best_bid)
    if not price.is_finite() or price <= 0:
        raise ValueError("invalid PAPER contract price")
    identifier = new_id("cst")
    request = OrderRequest(
        reference_id=identifier[:19],
        trading_symbol=instrument.trading_symbol,
        exchange=instrument.exchange,
        segment=instrument.segment,
        product=Product.MIS,
        order_type=OrderType.LIMIT,
        transaction_type=TransactionType.BUY,
        quantity=instrument.lot_size,
        price=price,
    )
    tariff = await CostStore(executor.clock).for_order(request, now)
    if tariff is None:
        raise ValueError("effective fee schedule unavailable")
    lot = instrument.lot_size
    margin = (
        executor.broker.account.requirement_for(
            lot, price, Product.MIS, instrument.segment, instrument.instrument_type
        )
        / lot
    )
    costs = ContractCosts(
        instrument_id=instrument.id,
        observed_at=quote.observed_at,
        available_at=now,
        source_id=identifier,
        data_origin=quote.data_origin,
        exposure_per_unit=price,
        margin_per_unit=margin,
        risk_cost_per_unit=source.risk_cost_reserve_per_unit,
        valid_until=min(source.valid_until, tariff.effective_to),
        fee_schedule=tariff,
        defined_max_loss_per_unit=price if long_option else None,
    )
    await AuditService(executor.clock).append(
        AuditIdentity(
            chain_id=identifier,
            event_type="PAPER_CONTRACT_OBSERVATION",
            actor="paper_contract_producer",
            mode=TradingMode.PAPER,
        ),
        {
            "instrument_id": instrument.id,
            "data_used": {
                "reference_publication_id": inputs.publication_id,
                "market_snapshot_id": observation.chain_id,
                "option_evidence": option_evidence.model_dump(mode="json") if long_option else None,
                "policy": source.model_dump(mode="json"),
                "fee_schedule": tariff.model_dump(mode="json"),
                "margin_model": asdict(executor.broker.account.margin_model),
                "fee_estimation": "SHARED_SIZER_USES_CANDIDATE_QUANTITY",
                "cost_status": "SIMULATED_MARGIN_AND_ESTIMATED_FEES_FINAL_PREFLIGHT_REQUIRED",
            },
            "result": costs.model_dump(mode="json"),
        },
    )
    return costs
