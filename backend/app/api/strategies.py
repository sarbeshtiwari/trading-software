"""Persisted strategy state and authenticated PAPER-only owner controls."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.api.risk import _paper_only
from app.core.enums import InstrumentType
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.modes import TradingMode
from app.portfolio.cost_store import CostStore
from app.portfolio.costs import FeeSchedule
from app.risk.active import active_limits
from app.strategies import monitor
from app.strategies.paper_policy import PaperEvidencePolicy, publish_policy, registration
from app.strategies.reference import ClosedCandleBreakout
from app.strategies.registry import StrategyRegistry
from app.strategies.review import StrategyEvidenceReview, review_strategy
from app.trading.inputs import ReferenceInputs, ReferenceInputStore

router = APIRouter(prefix="/strategies", tags=["strategies"])


class DegradationPolicyRequest(EvidenceModel):
    version: str = Field(min_length=1, max_length=32)
    policy: monitor.DegradationPolicy
    reason: str = Field(min_length=10, max_length=500)


class DegradationResetRequest(EvidenceModel):
    version: str = Field(min_length=1, max_length=32)
    policy_event_id: str = Field(min_length=1)
    last_event_id: str = Field(min_length=1)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["RESET STRATEGY DEGRADATION"]


@router.get("/{strategy_id}/degradation", response_model=monitor.DegradationView)
async def degradation(strategy_id: str, version: str):
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, version)
            return await monitor.review(session, row)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Strategy degradation evidence unavailable or corrupt") from None


@router.post("/{strategy_id}/degradation/policy")
async def configure_degradation(
    strategy_id: str, body: DegradationPolicyRequest, request: Request
) -> dict[str, str]:
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, body.version)
            event = await monitor.publish_policy(
                session,
                row,
                body.policy,
                actor=request.state.principal.username,
                reason=body.reason,
            )
            return {"audit_event_id": event.id}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Sequential policy and valid strategy evidence required") from None


@router.post("/{strategy_id}/degradation/reset")
async def reset_degradation(
    strategy_id: str, body: DegradationResetRequest, request: Request
) -> dict[str, str]:
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, body.version)
            event = await monitor.reset(
                session,
                row,
                policy_event_id=body.policy_event_id,
                last_event_id=body.last_event_id,
                actor=request.state.principal.username,
                reason=body.reason,
            )
            return {"audit_event_id": event.id}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(
            409, "Healthy evidence and current disable/policy review required"
        ) from None


class PaperPolicyRequest(EvidenceModel):
    version: str = Field(min_length=1, max_length=32)
    policy: PaperEvidencePolicy
    reason: str = Field(min_length=10, max_length=500)


class StrategyReviewRequest(EvidenceModel):
    version: str = Field(min_length=1, max_length=32)
    backtest_id: str | None = Field(default=None, pattern=r"^[a-z0-9]{8,26}$")
    walkforward_id: str | None = Field(default=None, pattern=r"^[a-z0-9]{8,26}$")
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["REVIEW STRATEGY EVIDENCE"]


@router.post("/{strategy_id}/evidence/policy")
async def configure_evidence(
    strategy_id: str, body: PaperPolicyRequest, request: Request
) -> dict[str, str]:
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, body.version)
            event = await publish_policy(
                session,
                row,
                body.policy,
                actor=request.state.principal.username,
                reason=body.reason,
            )
            return {"audit_event_id": event.id}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/{strategy_id}/evidence", response_model=StrategyEvidenceReview)
async def strategy_evidence(strategy_id: str, version: str):
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, version)
            return await review_strategy(
                session, row, backtest_id=row.backtest_run_id, walkforward_id=row.walkforward_run_id
            )
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Strategy evidence unavailable or integrity failure") from None


@router.post("/{strategy_id}/evidence/review", response_model=StrategyEvidenceReview)
async def record_strategy_review(strategy_id: str, body: StrategyReviewRequest, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            row = await registration(session, strategy_id, body.version)
            return await review_strategy(
                session,
                row,
                backtest_id=body.backtest_id,
                walkforward_id=body.walkforward_id,
                actor=request.state.principal.username,
                reason=body.reason,
            )
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Strategy evidence unavailable or integrity failure") from None


@router.get("")
async def strategies() -> list[dict]:
    return await StrategyRegistry().list()


class ReferenceRegistration(EvidenceModel):
    instrument_id: str = Field(min_length=1)
    kind: Literal["CASH", "LONG_OPTION"] = "CASH"
    reason: str = Field(min_length=10, max_length=500)


class ReferencePublication(EvidenceModel):
    inputs: ReferenceInputs
    reason: str = Field(min_length=10, max_length=500)


class StrategyEnablement(EvidenceModel):
    version: str = Field(min_length=1)
    enabled: bool
    reason: str = Field(min_length=10, max_length=500)


class TariffPublication(EvidenceModel):
    schedule: FeeSchedule
    reason: str = Field(min_length=10, max_length=500)


@router.post("/reference/fees")
async def publish_fees(body: TariffPublication, request: Request) -> dict[str, str]:
    _paper_only()
    try:
        identifier = await CostStore().publish(
            body.schedule, actor=request.state.principal.username, reason=body.reason
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"source_id": identifier}


@router.post("/reference/register")
async def register_reference(body: ReferenceRegistration, request: Request) -> dict[str, str]:
    _paper_only()
    async with db_session.session_scope() as session:
        instrument = await session.get(Instrument, body.instrument_id)
        limits = await active_limits(session)
    if (
        instrument is None
        or not instrument.is_active
        or instrument.is_restricted
        or instrument.instrument_type != (
            InstrumentType.OPTION if body.kind == "LONG_OPTION" else InstrumentType.EQUITY
        )
        or limits is None
    ):
        raise HTTPException(409, "Matching tradable instrument and active risk limits required")
    strategy = ClosedCandleBreakout(
        instrument.id,
        f"{instrument.exchange.value}:{instrument.trading_symbol}",
        instrument.tick_size,
        limits.per_trade_risk_pct / 100,
        long_option=body.kind == "LONG_OPTION",
    )
    try:
        identifier = await StrategyRegistry().register(
            strategy, actor=request.state.principal.username, reason=body.reason
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"registration_id": identifier, "strategy_id": strategy.spec.id, "version": "1"}


@router.post("/reference/inputs")
async def publish_inputs(body: ReferencePublication, request: Request) -> dict[str, str]:
    _paper_only()
    async with db_session.session_scope() as session:
        if await session.get(Instrument, body.inputs.instrument_id) is None:
            raise HTTPException(409, "Instrument unavailable")
    try:
        identifier = await ReferenceInputStore().publish(
            body.inputs, actor=request.state.principal.username, reason=body.reason
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"source_id": identifier}


@router.post("/{strategy_id}/paper")
async def enable_paper(
    strategy_id: str, body: StrategyEnablement, request: Request
) -> dict[str, bool]:
    _paper_only()
    try:
        await StrategyRegistry().set_enabled(
            strategy_id,
            body.version,
            TradingMode.PAPER,
            body.enabled,
            actor=request.state.principal.username,
            reason=body.reason,
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"enabled_paper": body.enabled}
