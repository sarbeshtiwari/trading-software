"""Read-only utilisation of authenticated, current risk-account evidence."""

from datetime import timedelta
from decimal import Decimal, localcontext

import sqlalchemy as sa
from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.core.clock import IST, UTC, get_clock
from app.core.data_origin import DataOrigin
from app.db.models.audit import AuditEvent
from app.db.models.system import SINGLETON_ID, SystemState
from app.modes import TradingMode
from app.risk.active import active_limits
from app.risk.budgets import daily_loss_limit, drawdown_limit, exposure_limit
from app.risk.config import RiskLimits
from app.risk.models import PortfolioState
from app.risk.safety import RiskSafety, SafetyState


class UtilisationMetric(EvidenceModel):
    name: str
    used: Decimal
    limit: Decimal
    percent: Decimal | None


class RiskUtilisation(EvidenceModel):
    mode: TradingMode
    origin: DataOrigin
    status: str
    observed_at: AwareDatetime | None = None
    evidence_id: str | None = None
    configuration_version: int | None = None
    strategy_id: str | None = None
    latch_code: str | None = None
    metrics: tuple[UtilisationMetric, ...] = ()


def account_metrics(account, limits):
    with localcontext() as context:
        context.prec = 50
        loss = max(Decimal(0), -account.realised_day_pnl - account.unrealised_day_pnl)
        daily = daily_loss_limit(account.equity, limits)
        values = (
            ("daily_loss", loss, daily),
            ("daily_loss_with_reserved_risk", loss + account.reserved_risk, daily),
            (
                "drawdown",
                account.peak_equity - account.equity,
                drawdown_limit(account.peak_equity, limits),
            ),
            (
                "gross_exposure",
                sum((item.notional for item in account.exposures), Decimal(0)),
                exposure_limit(account.equity, limits),
            ),
            (
                "open_and_pending_positions",
                Decimal(account.open_and_pending_positions),
                Decimal(limits.max_concurrent_positions),
            ),
            (
                "strategy_open_and_pending_positions",
                Decimal(account.strategy_open_and_pending_positions),
                Decimal(limits.max_positions_per_strategy),
            ),
        )
        return tuple(
            UtilisationMetric(
                name=name, used=used, limit=limit, percent=used / limit * 100 if limit else None
            )
            for name, used, limit in values
        )


async def utilisation(session, mode, origin):
    view = RiskUtilisation(mode=mode, origin=origin, status="ACCOUNT_EVIDENCE_UNAVAILABLE")
    limits = await active_limits(session)
    if limits is None:
        return view.model_copy(update={"status": "RISK_CONFIG_UNAVAILABLE"})
    view = view.model_copy(update={"configuration_version": limits.version})
    records = list(
        await session.scalars(
            sa.select(AuditEvent)
            .where(AuditEvent.chain_id == RiskSafety.chain_id(mode, origin))
            .order_by(AuditEvent.sequence)
        )
    )
    if not verify_records(records):
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_INTEGRITY_FAILURE"})
    now = get_clock().now()
    if any(
        (
            record.occurred_at.replace(tzinfo=UTC)
            if record.occurred_at.tzinfo is None
            else record.occurred_at
        )
        > now
        for record in records
    ):
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_INVALID"})
    source = next(
        (
            row
            for row in reversed(records)
            if row.event_type == "RISK_SAFETY" and row.data_used and "portfolio" in row.data_used
        ),
        None,
    )
    if source is None:
        return view
    try:
        account = PortfolioState.model_validate(source.data_used["portfolio"])
        recorded_limits = RiskLimits.model_validate(source.data_used["limits"])
        latches = SafetyState.model_validate(records[-1].result["state"])
    except (ValueError, TypeError, KeyError):
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_INVALID"})
    view = view.model_copy(
        update={
            "observed_at": account.observed_at,
            "evidence_id": source.id,
            "strategy_id": account.strategy_id,
            "latch_code": latches.code,
        }
    )
    system = await session.get(SystemState, SINGLETON_ID)
    return validated_view(view, account, limits, recorded_limits, source, system=system)


def validated_view(view, account, limits, recorded_limits, source, *, system):
    if (
        source.mode != view.mode
        or account.data_origin != view.origin
        or source.actor != "risk_safety"
    ):
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_INVALID"})
    if recorded_limits != limits:
        return view.model_copy(update={"status": "RISK_CONFIGURATION_CHANGED"})
    now = get_clock().now()
    maximum_age = timedelta(seconds=limits.max_portfolio_age_seconds)
    if not account.observed_at <= account.available_at <= now:
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_INVALID"})
    if (
        now - account.observed_at > maximum_age
        or now.astimezone(IST).date() != account.observed_at.astimezone(IST).date()
    ):
        return view.model_copy(update={"status": "ACCOUNT_EVIDENCE_STALE"})
    reconciled = system.last_reconciliation_at if system else None
    if reconciled is not None and reconciled.tzinfo is None:
        reconciled = reconciled.replace(tzinfo=UTC)
    if (
        system is None
        or system.mode != view.mode
        or system.open_discrepancies
        or reconciled is None
        or not account.observed_at <= reconciled <= now
        or now - reconciled > maximum_age
    ):
        return view.model_copy(update={"status": "ACCOUNT_UNRECONCILED"})
    return view.model_copy(
        update={"status": "AVAILABLE", "metrics": account_metrics(account, limits)}
    )
