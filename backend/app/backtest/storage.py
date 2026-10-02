"""Audited historical observations from the actual PAPER account and journal."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestTrade
from app.db.models.trading import Position, Trade
from app.execution.paper import stable_id
from app.modes import TradingMode


async def sample_account(session, worker, run_id, *, baseline=False):
    account = worker.executor.broker.account
    now = worker.clock.utcnow()
    positions = list(
        (await session.scalars(sa.select(Position).where(Position.net_quantity != 0))).all()
    )
    fills = list((await session.scalars(sa.select(Trade))).all())
    reasons = []
    if worker.failed:
        reasons.append("WORKER_REVIEW_REQUIRED")
    if any(
        not fill.cost_breakdown or fill.cost_breakdown.get("status") != "ESTIMATED"
        for fill in fills
    ):
        reasons.append("FILL_COSTS_UNAVAILABLE")
    if any(
        position.last_price is None
        or position.marked_at is None
        or not timedelta(0)
        <= now - _utc(position.marked_at)
        <= timedelta(seconds=worker.settings.tick_staleness_seconds)
        for position in positions
    ):
        reasons.append("POSITION_MARK_UNAVAILABLE_OR_STALE")
    values = {
        "observed_at": now.isoformat(),
        "baseline": baseline,
        "status": "UNAVAILABLE" if reasons else "ESTIMATED",
        "reasons": reasons,
        "net_equity": str(account.equity) if not reasons else None,
        "cash": str(account.cash) if not reasons else None,
        "gross_realised_pnl": str(account.realised_pnl),
        "charges": str(account.charges_paid) if "FILL_COSTS_UNAVAILABLE" not in reasons else None,
        "unrealised_pnl": str(account.unrealised_pnl()) if not reasons else None,
        "gross_exposure": str(
            sum(
                (abs(position.net_quantity) * position.last_price for position in positions),
                Decimal(0),
            )
        )
        if not reasons
        else None,
        "used_margin": str(account.used_margin),
        "position_ids": [position.id for position in positions],
        "data_origin": "REPLAY",
        "execution_realism": "SIMULATED",
    }
    await AuditService(worker.clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=f"history:{run_id}",
            event_type="HISTORICAL_ACCOUNT_SAMPLE",
            actor="historical-runner",
            mode=TradingMode.PAPER,
        ),
        {"result": values},
    )


async def stored_samples(session, run_id):
    rows = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == f"history:{run_id}",
                    AuditEvent.event_type == "HISTORICAL_ACCOUNT_SAMPLE",
                )
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    return [row.result | {"audit_event_id": row.id} for row in rows]


def copy_journal_trades(session, run_id, entries):
    links = []
    for entry in entries:
        if any(
            value is None
            for value in (
                entry.trading_symbol,
                entry.direction,
                entry.quantity,
                entry.opened_at,
                entry.actual_entry,
                entry.closed_at,
                entry.actual_exit,
            )
        ):
            raise ValueError("historical trade journal lineage incomplete")
        identifier = stable_id("btt", f"{run_id}:{entry.id}")
        session.add(
            BacktestTrade(
                id=identifier,
                run_id=run_id,
                trading_symbol=entry.trading_symbol,
                direction=entry.direction.value,
                quantity=entry.quantity,
                entry_ts=entry.opened_at,
                entry_price=entry.actual_entry,
                exit_ts=entry.closed_at,
                exit_price=entry.actual_exit,
                exit_reason=entry.exit_reason,
                stop_loss=entry.planned_stop,
                target=entry.planned_target,
                gross_pnl=entry.gross_pnl,
                charges=entry.charges,
                net_pnl=entry.net_pnl,
                r_multiple=entry.r_multiple,
            )
        )
        links.append(
            {
                "trade_id": identifier,
                "journal_id": entry.id,
                "proposal_id": entry.proposal_id,
                "risk_decision_id": entry.risk_decision_id,
                "position_id": entry.position_id,
                "entry_order_id": entry.entry_order_id,
                "exit_order_ids": entry.exit_order_ids,
                "audit_chain_id": entry.audit_chain_id,
            }
        )
    return links
