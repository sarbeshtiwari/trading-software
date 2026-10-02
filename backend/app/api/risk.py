"""Owner-only PAPER controls; re-arm requires fresh server-held account evidence."""

from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db import session as db_session
from app.db.models.audit import AuditEvent, ConfigChange
from app.db.models.config import RiskConfigVersion
from app.db.models.decision import RiskDecision
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.system import SINGLETON_ID, SystemState
from app.fno.restrictions import BanAdmission, BanState
from app.fno.restrictions import admit as admit_ban
from app.fno.restrictions import state_at as ban_state
from app.modes import TradingMode
from app.news.reactions import ReactionPolicy, ReactionPolicyView
from app.news.reactions import configure as configure_reactions
from app.news.reactions import policy_at as reaction_policy
from app.risk.active import active_limits
from app.risk.config import RiskLimits, stricter
from app.risk.event_controls import EventControlChange, EventControlState
from app.risk.event_controls import list_states as event_states
from app.risk.event_controls import publish as publish_events
from app.risk.event_controls import state_at as event_state
from app.risk.inspection import RiskDecisionInspection, RiskDecisionPage, index, inspect_decision
from app.risk.instrument_blocks import InstrumentBlockChange, InstrumentBlockState, list_states
from app.risk.instrument_blocks import configure as configure_instrument_block
from app.risk.instrument_blocks import state as instrument_block_state
from app.risk.models import PortfolioState
from app.risk.news_halts import NewsHaltState, release, state_at
from app.risk.safety import RiskSafety
from app.risk.state import RiskUtilisation, utilisation

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/decisions", response_model=RiskDecisionPage)
async def list_risk_decisions(offset: int = Query(default=0, ge=0)):
    async with db_session.session_scope() as session:
        rows = list(
            await session.scalars(
                sa.select(RiskDecision)
                .where(
                    RiskDecision.mode == get_settings().trading_mode,
                    RiskDecision.evaluated_at <= get_clock().utcnow(),
                )
                .order_by(RiskDecision.evaluated_at.desc(), RiskDecision.id.desc())
                .offset(offset)
                .limit(51)
            )
        )
    return RiskDecisionPage(items=tuple(index(row) for row in rows[:50]), has_more=len(rows) > 50)


@router.get("/decisions/{identifier}", response_model=RiskDecisionInspection)
async def read_risk_decision(identifier: str):
    if not 1 <= len(identifier) <= 40:
        raise HTTPException(422, "Invalid decision identifier")
    async with db_session.session_scope() as session:
        result = await inspect_decision(session, identifier, get_settings().trading_mode)
    if result is None:
        raise HTTPException(404, "Risk decision unavailable")
    return result


@router.get("/utilisation", response_model=RiskUtilisation)
async def read_utilisation(origin: DataOrigin):
    async with db_session.session_scope() as session:
        return await utilisation(session, get_settings().trading_mode, origin)


@router.get("/fno-bans", response_model=BanState)
async def read_fno_bans(origin: DataOrigin):
    try:
        async with db_session.session_scope() as session:
            return await ban_state(session, origin)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "F&O ban evidence unavailable") from None


@router.put("/fno-bans", response_model=BanState)
async def publish_fno_bans(body: BanAdmission, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await admit_ban(session, body, actor=request.state.principal.username)
    except (ValueError, KeyError, TypeError, sa.exc.IntegrityError, sa.exc.OperationalError):
        raise HTTPException(409, "F&O ban report invalid, changed or unavailable") from None


@router.get("/event-controls", response_model=list[EventControlState])
async def event_controls():
    async with db_session.session_scope() as session:
        return await event_states(session)


@router.get("/event-controls/{instrument_id}", response_model=EventControlState)
async def read_event_control(
    instrument_id: str, origin: DataOrigin, strategy_id: str | None = None
):
    try:
        async with db_session.session_scope() as session:
            return await event_state(session, instrument_id, origin, strategy_id=strategy_id)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Event control evidence unavailable") from None


@router.put("/event-controls/{instrument_id}", response_model=EventControlState)
async def publish_event_control(instrument_id: str, body: EventControlChange, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await publish_events(
                session, instrument_id, body, actor=request.state.principal.username
            )
    except (ValueError, KeyError, TypeError, sa.exc.IntegrityError, sa.exc.OperationalError):
        raise HTTPException(409, "Event control changed, invalid or unavailable") from None


@router.get("/instrument-blocks", response_model=list[InstrumentBlockState])
async def instrument_blocks():
    async with db_session.session_scope() as session:
        return await list_states(session)


@router.get("/instrument-blocks/{instrument_id}", response_model=InstrumentBlockState)
async def read_instrument_block(instrument_id: str):
    try:
        async with db_session.session_scope() as session:
            return await instrument_block_state(session, instrument_id)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Instrument control evidence unavailable") from None


@router.put("/instrument-blocks/{instrument_id}", response_model=InstrumentBlockState)
async def change_instrument_block(
    instrument_id: str, body: InstrumentBlockChange, request: Request
):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await configure_instrument_block(
                session, instrument_id, body, actor=request.state.principal.username
            )
    except (ValueError, KeyError, TypeError, sa.exc.IntegrityError, sa.exc.OperationalError):
        raise HTTPException(409, "Instrument control changed or evidence unavailable") from None


class NewsReactionChange(EvidenceModel):
    policy: ReactionPolicy
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)


class NewsHaltRelease(EvidenceModel):
    origin: DataOrigin
    expected_event_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


@router.get("/news-policy", response_model=ReactionPolicyView)
async def get_news_policy():
    async with db_session.session_scope() as session:
        return await reaction_policy(session)


@router.put("/news-policy", response_model=ReactionPolicyView)
async def set_news_policy(body: NewsReactionChange, request: Request):
    _paper_only()
    try:
        async with db_session.session_scope() as session:
            return await configure_reactions(
                session,
                body.policy,
                expected_event_id=body.expected_event_id,
                reason=body.reason,
                actor=request.state.principal.username,
            )
    except (ValueError, KeyError, TypeError, sa.exc.IntegrityError, sa.exc.OperationalError):
        raise HTTPException(409, "News policy changed or evidence unavailable") from None


@router.get("/news-halts", response_model=list[NewsHaltState])
async def news_halts():
    async with db_session.session_scope() as session:
        events = (
            await session.scalars(
                sa.select(AuditEvent).where(
                    AuditEvent.event_type == "NEWS_INSTRUMENT_HALT",
                    AuditEvent.mode == TradingMode.PAPER,
                    AuditEvent.occurred_at <= get_clock().utcnow(),
                )
            )
        ).all()
        identities = {(event.instrument_id, event.result["cause"]["origin"]) for event in events}
        return [
            await state_at(session, instrument, origin) for instrument, origin in sorted(identities)
        ]


@router.post("/news-halts/{instrument_id}/release", response_model=NewsHaltState)
async def release_news_halt(instrument_id: str, body: NewsHaltRelease, request: Request):
    _paper_only()
    if body.confirmation != "REVIEWED NEWS HALT":
        raise HTTPException(422, "Explicit review confirmation required")
    try:
        async with db_session.session_scope() as session:
            return await release(
                session,
                instrument_id,
                body.origin,
                expected_event_id=body.expected_event_id,
                reason=body.reason,
                actor=request.state.principal.username,
            )
    except (ValueError, KeyError, TypeError, sa.exc.IntegrityError, sa.exc.OperationalError):
        raise HTTPException(409, "News halt changed or evidence unavailable") from None


class ConfigurationChange(EvidenceModel):
    limits: RiskLimits
    expected_version: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=10, max_length=500)


class Rearm(EvidenceModel):
    origin: DataOrigin
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str


def _paper_only():
    if get_settings().trading_mode != TradingMode.PAPER:
        raise HTTPException(423, "Only PAPER control mutations are implemented")


@router.get("")
async def state():
    async with db_session.session_scope() as session:
        limits = await active_limits(session)
    safety = RiskSafety()
    states = {
        origin.value: (await safety.restore(get_settings().trading_mode, origin)).model_dump(
            mode="json"
        )
        for origin in DataOrigin
    }
    return {
        "limits": limits.model_dump(mode="json") if limits else None,
        "status": "AVAILABLE" if limits else "RISK_CONFIG_UNAVAILABLE",
        "latches": states,
        "gate": safety.gate.state.to_dict(),
        "rearm_requires": "FRESH_RECONCILED_SERVER_ACCOUNT_STATE",
    }


@router.post("/configuration")
async def configure(body: ConfigurationChange, request: Request):
    _paper_only()
    settings, clock = get_settings(), get_clock()
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    sa.select(RiskConfigVersion)
                    .order_by(RiskConfigVersion.version)
                    .with_for_update()
                )
            ).all()
        )
        version = rows[-1].version if rows else 0
        if body.expected_version != version or body.limits.version != version + 1:
            raise HTTPException(409, "Risk configuration version changed")
        before = await active_limits(session)
        for row in rows:
            row.is_active = False
        limits = body.limits
        session.add(
            RiskConfigVersion(
                version=limits.version,
                is_active=True,
                capital=limits.capital,
                per_trade_risk_pct=limits.per_trade_risk_pct,
                daily_loss_limit_pct=limits.daily_loss_limit_pct,
                max_drawdown_pct=limits.max_drawdown_pct,
                max_gross_exposure_multiple=limits.max_gross_exposure_multiple,
                max_concurrent_positions=limits.max_concurrent_positions,
                max_trades_per_day=settings.max_trades_per_day,
                min_reward_risk_ratio=limits.min_reward_risk_ratio,
                margin_buffer_pct=limits.margin_buffer_pct,
                max_slippage_pct=settings.max_slippage_pct,
                extra_limits={"engine_limits": limits.model_dump(mode="json")},
                author=request.state.principal.username,
                reason=body.reason,
                activated_at=clock.utcnow(),
            )
        )
        session.add(
            ConfigChange(
                target="risk_config",
                target_id=str(limits.version),
                action="ACTIVATE",
                before=before.model_dump(mode="json") if before else None,
                after=limits.model_dump(mode="json"),
                reason=body.reason,
                actor=request.state.principal.username,
                occurred_at=clock.utcnow(),
            )
        )
        await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id="risk-configuration",
                event_type="RISK_CONFIGURATION",
                actor=request.state.principal.username,
                mode=settings.trading_mode,
            ),
            {
                "result": {
                    "before": before.model_dump(mode="json") if before else None,
                    "after": limits.model_dump(mode="json"),
                    "reason": body.reason,
                }
            },
        )
    return {"version": limits.version}


@router.post("/rearm")
async def rearm(body: Rearm, request: Request):
    _paper_only()
    if body.confirmation != "REARM PAPER RISK":
        raise HTTPException(422, "Explicit confirmation required")
    safety, clock = RiskSafety(), get_clock()
    mode = get_settings().trading_mode
    chain = safety.chain_id(mode, body.origin)
    async with db_session.session_scope() as session:
        before, count = await safety._load(session, chain)
        limits = await active_limits(session)
        system = await session.get(SystemState, SINGLETON_ID, with_for_update=True)
        if (
            limits is None
            or system is None
            or system.mode != mode
            or system.last_reconciliation_at is None
            or system.open_discrepancies
        ):
            raise HTTPException(409, "Fresh reconciliation and active risk configuration required")
        reconciled = system.last_reconciliation_at
        if reconciled.tzinfo is None:
            reconciled = reconciled.replace(tzinfo=UTC)
        if (
            not timedelta(0)
            <= clock.now() - reconciled
            <= timedelta(seconds=limits.max_portfolio_age_seconds)
        ):
            raise HTTPException(409, "Reconciliation is stale or future dated")
        events = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == chain)
                    .order_by(AuditEvent.sequence.desc())
                )
            ).all()
        )
        source = next(
            (row for row in events if row.data_used and "portfolio" in row.data_used), None
        )
        if source is None:
            raise HTTPException(409, "Trusted account evidence unavailable")
        account = PortfolioState.model_validate(source.data_used["portfolio"])
        if (
            account.data_origin != body.origin
            or not account.observed_at <= account.available_at <= clock.now()
            or clock.now() - account.observed_at
            > timedelta(seconds=limits.max_portfolio_age_seconds)
            or reconciled < account.observed_at
        ):
            raise HTTPException(409, "Account evidence is stale or unreconciled")
        drawdown_limit = stricter(
            account.peak_equity * limits.max_drawdown_pct / 100, limits.max_drawdown_amount
        )
        if account.peak_equity - account.equity >= drawdown_limit:
            raise HTTPException(423, "Drawdown remains beyond limit")
        after = before.model_copy(update={"drawdown": False, "engine_error": False})
        record = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=chain,
                event_type="RISK_REARM",
                actor=request.state.principal.username,
                mode=mode,
            ),
            {
                "data_used": {"account_event_id": source.id, "reconciled_at": reconciled},
                "result": {
                    "before": before.model_dump(mode="json"),
                    "state": after.model_dump(mode="json"),
                    "reason": body.reason,
                },
            },
            expected_count=count,
        )
        session.add(RuntimeEventOutbox(audit_id=record.id))
        session.add(
            ConfigChange(
                target="risk_latch",
                target_id=chain,
                action="REARM",
                before=before.model_dump(mode="json"),
                after=after.model_dump(mode="json"),
                reason=body.reason,
                actor=request.state.principal.username,
                occurred_at=clock.utcnow(),
            )
        )
    safety._publish(chain, after)
    return {"state": after.model_dump(mode="json"), "gate": safety.gate.state.to_dict()}
