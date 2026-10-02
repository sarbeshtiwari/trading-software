"""Typed, read-only application state. Missing telemetry is never synthesized."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import AliasPath, BaseModel, ConfigDict, Field, field_validator

from app.analysis.regime.classifier import RegimeDecision, evidence_fresh_at
from app.config import get_settings
from app.core.clock import UTC, get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.db.models.decision import ConsideredCandidate, RiskDecision
from app.db.models.journal import JournalEntry
from app.db.models.llm import LLMBudgetDay, LLMCall
from app.db.models.market_data import OptionChainSnapshot
from app.db.models.regime import RegimeHistory
from app.db.models.system import PortfolioSnapshot
from app.db.models.trading import Order, Position, Trade
from app.execution.protection import protection_status
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.notifications.outbox import recorded_states
from app.notifications.runtime import current_service
from app.risk.entry_models import EntryEvidence
from app.trading.worker import worker_status

router = APIRouter(prefix="/workspace", tags=["workspace"])


class Row(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def explicit_utc(cls, value):
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value


class PositionView(Row):
    id: str
    segment: str
    trading_symbol: str
    state: str
    net_quantity: int
    average_price: Decimal
    last_price: Decimal | None
    marked_at: datetime | None
    stop_loss_price: Decimal | None
    target_price: Decimal | None
    trailing_stop_price: Decimal | None
    is_protected: bool
    watchdog_observation: str = "UNAVAILABLE"
    realised_pnl: Decimal
    unrealised_pnl: Decimal | None
    mode: str
    execution_realism: str


class OrderView(Row):
    id: str
    role: str
    segment: str
    trading_symbol: str
    status: str
    quantity: int
    filled_quantity: int
    average_fill_price: Decimal | None
    rejection_reason: str | None
    created_at: datetime
    proposal_id: str | None
    mode: str
    execution_realism: str


class FillView(Row):
    id: str
    order_id: str
    position_id: str | None
    transaction_type: str
    quantity: int
    price: Decimal
    executed_at: datetime
    brokerage: Decimal
    taxes: Decimal
    other_charges: Decimal
    cost_breakdown: dict[str, Any] | None
    mode: str
    execution_realism: str


class AccountView(Row):
    ts: datetime
    equity: Decimal
    cash: Decimal | None
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    gross_exposure: Decimal
    available_margin: Decimal | None
    charges: Decimal
    mode: str
    pnl_basis: Literal["GROSS_REALIZED_KNOWN_CHARGES_DEDUCTED_FROM_EQUITY"] = (
        "GROSS_REALIZED_KNOWN_CHARGES_DEDUCTED_FROM_EQUITY"
    )


class DecisionView(Row):
    id: str
    trading_symbol: str
    strategy_id: str | None
    stopped_at_stage: str
    reason_code: str
    created_at: datetime
    mode: str


class AuditView(Row):
    id: str
    chain_id: str
    event_type: str
    occurred_at: datetime
    severity: str
    actor: str | None
    decision: str | None
    mode: str | None


class PreflightView(Row):
    id: str
    proposal_id: str | None
    approved: bool
    approved_quantity: int | None
    rejection_code: str | None
    binding_rule: str | None
    evaluated_at: datetime
    mode: str
    entry_policy: EntryEvidence | None = Field(
        default=None, validation_alias=AliasPath("state_snapshot", "decision", "entry_policy")
    )


class JournalView(Row):
    id: str
    kind: str
    rejection_code: str | None
    rejection_rule: str | None
    proposal_id: str | None
    entry_order_id: str | None
    exit_order_ids: list[str] | None
    position_id: str | None
    strategy_id: str | None
    regime_at_entry: str | None
    regime_id: str | None = Field(
        default=None,
        validation_alias=AliasPath("indicator_snapshot", "decision_context", "regime_id"),
    )
    trading_symbol: str | None
    gross_pnl: Decimal | None
    charges: Decimal | None
    net_pnl: Decimal | None
    exit_reason: str | None
    closed_at: datetime | None
    mode: str


class StrategyView(Row):
    strategy_id: str
    version: str
    enabled_paper: bool
    auto_disabled: bool


class ChainView(Row):
    underlying: str
    ts: datetime
    spot_price: Decimal | None
    pcr_oi: Decimal | None
    pcr_volume: Decimal | None
    max_pain_strike: Decimal | None
    data_origin: str


class ComponentView(BaseModel):
    name: str
    status: str
    detail: str


class NotificationView(Row):
    event_id: str
    source_audit_id: str
    event_type: str
    requested_at: datetime
    order_id: str | None
    position_id: str | None
    status: str
    channels: dict[str, str]
    attempts: int
    external_delivery_verified: Literal[False]


class RegimeView(BaseModel):
    id: str
    underlying: str
    as_of: datetime
    data_origin: str
    label: str
    candidate: str
    reason: str


class AdvisoryCallView(Row):
    id: str
    provider: str
    model_id: str
    purpose: str
    prompt_version: str
    outcome: str
    cost_usd: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    proposal_id: str | None
    correlation_id: str | None
    called_at: datetime


class AdvisoryBudgetView(BaseModel):
    review_enabled: bool
    cost_basis: Literal["OWNER_TARIFF_ESTIMATE_NOT_PROVIDER_INVOICE"]
    utc_day: str
    reserved_usd: Decimal
    accounted_usd: Decimal
    reserved_tokens: int
    accounted_tokens: int
    daily_cost_cap_usd: Decimal
    daily_token_cap: int


class WorkspaceView(BaseModel):
    trading_mode: str
    generated_at: datetime
    new_entries_allowed: bool
    trading_enabled: bool
    blockers: list[str]
    broker_verification: Literal["GROWW_LIVE_UNVERIFIED"]
    account: AccountView | None
    account_status: Literal["SNAPSHOT_ONLY", "UNAVAILABLE"]
    regime_status: str
    regime: RegimeView | None = None
    positions: list[PositionView]
    orders: list[OrderView]
    fills: list[FillView]
    decisions: list[DecisionView]
    advisory_calls: list[AdvisoryCallView] = Field(default_factory=list)
    advisory_budget: AdvisoryBudgetView | None = None
    audit: list[AuditView]
    preflights: list[PreflightView]
    journal: list[JournalView]
    strategies: list[StrategyView]
    chains: list[ChainView]
    components: list[ComponentView]
    notifications: list[NotificationView]


@router.get("", response_model=WorkspaceView)
async def workspace():
    mode, now = get_settings().trading_mode, get_clock().utcnow()
    async with db_session.session_scope() as session:
        quota = await session.get(LLMBudgetDay, now.date())
        settings = get_settings()
        advisory_budget = AdvisoryBudgetView(
            review_enabled=settings.llm_reference_review_enabled,
            cost_basis="OWNER_TARIFF_ESTIMATE_NOT_PROVIDER_INVOICE",
            utc_day=now.date().isoformat(),
            reserved_usd=Decimal(quota.reserved_microusd) / 1000000 if quota else Decimal(0),
            accounted_usd=Decimal(quota.spent_microusd) / 1000000 if quota else Decimal(0),
            reserved_tokens=quota.reserved_tokens if quota else 0,
            accounted_tokens=quota.spent_tokens if quota else 0,
            daily_cost_cap_usd=settings.llm_daily_cost_cap_usd,
            daily_token_cap=settings.llm_daily_token_cap,
        )
        advisory_calls = list(
            (
                await session.scalars(
                    sa.select(LLMCall)
                    .where(
                        LLMCall.called_at <= now,
                        sa.exists().where(
                            AuditEvent.chain_id == LLMCall.id,
                            AuditEvent.event_type.in_(("ADVISORY_PROPOSAL", "ADVISORY_REQUEST")),
                            AuditEvent.mode == mode,
                        ),
                    )
                    .order_by(LLMCall.called_at.desc(), LLMCall.id.desc())
                    .limit(200)
                )
            ).all()
        )
        notification_states = await recorded_states(session, now)
        account = await session.scalar(
            sa.select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.mode == mode,
                PortfolioSnapshot.ts <= now,
            )
            .order_by(PortfolioSnapshot.ts.desc(), PortfolioSnapshot.id.desc())
            .limit(1)
        )
        positions = list(
            (
                await session.scalars(sa.select(Position).where(Position.mode == mode).limit(200))
            ).all()
        )
        orders = list(
            (
                await session.scalars(
                    sa.select(Order)
                    .where(Order.mode == mode)
                    .order_by(Order.created_at.desc())
                    .limit(200)
                )
            ).all()
        )
        fills = list(
            (
                await session.scalars(
                    sa.select(Trade)
                    .where(
                        Trade.mode == mode,
                        Trade.executed_at <= now,
                    )
                    .order_by(Trade.executed_at.desc(), Trade.id.desc())
                    .limit(200)
                )
            ).all()
        )
        decisions = list(
            (
                await session.scalars(
                    sa.select(ConsideredCandidate)
                    .where(ConsideredCandidate.mode == mode)
                    .order_by(ConsideredCandidate.created_at.desc())
                    .limit(200)
                )
            ).all()
        )
        audit = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.mode == mode)
                    .order_by(AuditEvent.occurred_at.desc())
                    .limit(200)
                )
            ).all()
        )
        strategies = list((await session.scalars(sa.select(StrategyRegistration).limit(200))).all())
        preflights = list(
            (
                await session.scalars(
                    sa.select(RiskDecision)
                    .where(RiskDecision.mode == mode, RiskDecision.is_preflight.is_(True))
                    .order_by(RiskDecision.evaluated_at.desc())
                    .limit(200)
                )
            ).all()
        )
        journal = list(
            (
                await session.scalars(
                    sa.select(JournalEntry)
                    .where(JournalEntry.mode == mode, JournalEntry.version == 1)
                    .order_by(JournalEntry.created_at.desc())
                    .limit(200)
                )
            ).all()
        )
        chains = list(
            (
                await session.scalars(
                    sa.select(OptionChainSnapshot)
                    .where(OptionChainSnapshot.ts <= now)
                    .order_by(OptionChainSnapshot.ts.desc())
                    .limit(20)
                )
            ).all()
        )
        regime = await session.scalar(
            sa.select(RegimeHistory)
            .where(RegimeHistory.ts <= now, RegimeHistory.created_at <= now)
            .order_by(RegimeHistory.ts.desc())
            .limit(1)
        )
        protection_states = {
            position.id: await protection_status(session, position, now)
            if position.net_quantity
            else "FLAT"
            for position in positions
        }
    position_views = []
    for position in positions:
        view = PositionView.model_validate(position)
        view.watchdog_observation = protection_states[position.id]
        if position.marked_at is None or position.last_price is None:
            view.unrealised_pnl = None
        position_views.append(view)
    report = get_health_registry().last_report
    components = (
        [
            ComponentView(name=item.name, status=item.status.value, detail=item.detail)
            for item in report.results
        ]
        if report
        else []
    )
    components += [
        ComponentView(name="scheduler", **worker_status()),
        ComponentView(
            name="notifications",
            status=(
                "DEGRADED"
                if not current_service().channels
                or any(
                    "FAILED" in row["channels"].values() or row["status"] == "INTEGRITY_FAILURE"
                    for row in notification_states
                )
                else "RUNNING"
            )
            if current_service()
            else "DISABLED",
            detail="Provider delivery is not externally verified",
        ),
    ]
    gate = get_trading_gate()
    regime_view = None
    regime_status = "UNAVAILABLE"
    if regime is not None:
        try:
            decision = RegimeDecision.model_validate(regime.decision)
            regime_status = (
                "RECORDED_SNAPSHOT" if evidence_fresh_at(decision, now) else "STALE_OR_INCOMPLETE"
            )
            regime_view = {
                "id": regime.id,
                "underlying": regime.underlying,
                "as_of": decision.inputs.as_of.isoformat(),
                "data_origin": regime.data_origin,
                "label": decision.label.value,
                "candidate": decision.candidate.value,
                "reason": decision.reason,
            }
        except ValueError:
            regime_status = "INVALID_SNAPSHOT"
    return WorkspaceView(
        trading_mode=mode.value,
        generated_at=now.astimezone(UTC),
        new_entries_allowed=gate.new_entries_allowed,
        trading_enabled=gate.trading_enabled,
        blockers=list(gate.state.reasons),
        broker_verification="GROWW_LIVE_UNVERIFIED",
        account=AccountView.model_validate(account) if account else None,
        account_status="SNAPSHOT_ONLY" if account else "UNAVAILABLE",
        regime_status=regime_status,
        regime=regime_view,
        positions=position_views,
        orders=[OrderView.model_validate(row) for row in orders],
        fills=[FillView.model_validate(row) for row in fills],
        decisions=[DecisionView.model_validate(row) for row in decisions],
        advisory_calls=[AdvisoryCallView.model_validate(row) for row in advisory_calls],
        advisory_budget=advisory_budget,
        audit=[AuditView.model_validate(row) for row in audit],
        preflights=[PreflightView.model_validate(row) for row in preflights],
        journal=[JournalView.model_validate(row) for row in journal],
        strategies=[StrategyView.model_validate(row) for row in strategies],
        chains=[ChainView.model_validate(row) for row in chains],
        components=components,
        notifications=[NotificationView.model_validate(row) for row in notification_states],
    )
