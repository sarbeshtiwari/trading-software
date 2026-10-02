"""Long-option entry authorization from the original audited contract policy."""

import sqlalchemy as sa

from app.agents.pipeline import ContractCosts
from app.agents.validation import _utc
from app.audit.service import AuditService
from app.core.enums import Exchange, Product, Segment, SignalDirection
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.fno.expiry import select_expiry
from app.fno.options import option_contract
from app.modes import TradingMode
from app.trading.contracts import LongOptionContractSource
from app.trading.inputs import ReferenceInputStore


async def require_long_option_policy(proposal, instrument, context, now):
    if (
        instrument is None
        or not instrument.is_option
        or proposal.mode != TradingMode.PAPER
        or proposal.segment != Segment.FNO
        or instrument.segment != proposal.segment
        or instrument.exchange != Exchange.NSE
        or instrument.trading_symbol != proposal.trading_symbol
        or proposal.direction != SignalDirection.LONG
        or proposal.product != Product.MIS
        or proposal.strategy_id != "long-option-breakout"
    ):
        raise SafetyError("UNSUPPORTED_PAPER_FNO_STRUCTURE")
    async with db_session.session_scope() as session:
        row = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.chain_id == context.costs.source_id,
                AuditEvent.event_type == "PAPER_CONTRACT_OBSERVATION",
            )
        )
    if (
        row is None
        or row.instrument_id != instrument.id
        or row.mode != TradingMode.PAPER
        or row.actor != "paper_contract_producer"
        or _utc(row.occurred_at) > now
        or not await AuditService().verify(row.chain_id, expected_count=1)
    ):
        raise SafetyError("OPTION_CONTRACT_SOURCE_UNAVAILABLE")
    captured = ContractCosts.model_validate(row.result)
    if (
        captured.model_dump(exclude={"risk_cost_per_unit"})
        != context.costs.model_dump(exclude={"risk_cost_per_unit"})
        or context.costs.risk_cost_per_unit < captured.risk_cost_per_unit
        or context.market.greeks is None
        or row.data_used.get("option_evidence") != context.market.greeks.model_dump(mode="json")
        or captured.fee_schedule is None
        or captured.fee_schedule.charge_basis != "OPTION_PREMIUM"
    ):
        raise SafetyError("OPTION_CONTRACT_SOURCE_CHANGED")
    published = await ReferenceInputStore().at(instrument.id, context.market.data_origin, now)
    if (
        published is None
        or published.publication_id != row.data_used.get("reference_publication_id")
        or not isinstance(published.contract_source, LongOptionContractSource)
        or published.contract_source.model_dump(mode="json") != row.data_used.get("policy")
        or not published.contract_source.valid_at(now)
    ):
        raise SafetyError("OPTION_CONTRACT_POLICY_CHANGED_OR_EXPIRED")
    if (
        select_expiry(
            [option_contract(instrument)],
            as_of=now,
            min_days=published.contract_source.minimum_days_to_expiry,
        )
        != instrument.expiry_date
    ):
        raise SafetyError("OPTION_MINIMUM_DTE")
