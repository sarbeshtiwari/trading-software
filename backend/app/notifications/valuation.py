"""Read-only session-end valuation evidence from the reconciled PAPER lifecycle."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.db.models.system import SINGLETON_ID, PortfolioSnapshot, SystemState
from app.db.models.trading import Trade
from app.modes import TradingMode
from app.risk.budgets import daily_loss_limit, drawdown_limit, exposure_limit


def usage(amount, limit):
    return {
        "amount": amount,
        "limit": limit,
        "utilisation_pct": amount / limit * 100 if limit > 0 else None,
        "at_or_above_limit": amount >= limit,
    }


async def session_valuation(
    session, worker, *, positions, limits, cutoff, net_realised, cycle_error
):
    unavailable = {"status": "UNAVAILABLE", "reason": None}
    if cycle_error or not worker.executor.ready or limits is None:
        return {
            **unavailable,
            "reason": "RISK_CONFIGURATION_UNAVAILABLE"
            if limits is None
            else "RECONCILIATION_NOT_CONFIRMED",
        }
    system = await session.get(SystemState, SINGLETON_ID)
    max_age = timedelta(seconds=worker.settings.tick_staleness_seconds)
    if (
        system is None
        or system.last_reconciliation_at is None
        or not timedelta(0) <= cutoff - _utc(system.last_reconciliation_at) <= max_age
        or system.open_discrepancies
    ):
        return {**unavailable, "reason": "RECONCILIATION_EVIDENCE_STALE"}
    history = list(
        (await session.scalars(sa.select(Trade).where(Trade.mode == TradingMode.PAPER))).all()
    )
    if any(_utc(trade.executed_at) > cutoff for trade in history):
        return {**unavailable, "reason": "FUTURE_ACCOUNT_FILL"}
    if net_realised is None or any(
        not trade.cost_breakdown or trade.cost_breakdown.get("status") != "ESTIMATED"
        for trade in history
    ):
        return {**unavailable, "reason": "ACCOUNT_CHARGES_UNAVAILABLE"}
    account = worker.executor.broker.account
    marks, reason = position_marks(account, positions, cutoff, max_age)
    if reason:
        return {**unavailable, "reason": reason}
    snapshots = list(
        (
            await session.scalars(
                sa.select(PortfolioSnapshot).where(
                    PortfolioSnapshot.mode == TradingMode.PAPER, PortfolioSnapshot.ts <= cutoff
                )
            )
        ).all()
    )
    equity = account.equity
    peak = max(account.starting_capital, equity, *(row.equity for row in snapshots))
    unrealised = account.unrealised_pnl()
    exposure = sum((abs(mark["quantity"]) * mark["price"] for mark in marks), Decimal(0))
    return {
        "status": "ESTIMATED",
        "reason": None,
        "reconciled_at": _utc(system.last_reconciliation_at),
        "account_fill_ids": [trade.id for trade in history],
        "peak_snapshot_ids": [row.id for row in snapshots],
        "marks": marks,
        "equity": equity,
        "cash": account.cash,
        "peak_equity": peak,
        "unrealised_pnl": unrealised,
        "gross_exposure": exposure,
        "daily_loss": usage(
            max(Decimal(0), -net_realised - unrealised),
            daily_loss_limit(max(Decimal(0), equity), limits),
        ),
        "drawdown": usage(peak - equity, drawdown_limit(peak, limits)),
        "exposure": usage(exposure, exposure_limit(max(Decimal(0), equity), limits)),
    }


def position_marks(account, positions, cutoff, max_age):
    marks = []
    for position in positions:
        if (
            position.last_price is None
            or position.last_price <= 0
            or position.marked_at is None
            or not timedelta(0) <= cutoff - _utc(position.marked_at) <= max_age
        ):
            return [], "POSITION_MARK_STALE_OR_UNAVAILABLE"
        matching = [
            item
            for item in account.open_positions()
            if (item.trading_symbol, item.segment, item.product)
            == (position.trading_symbol, position.segment, position.product)
        ]
        if len(matching) != 1 or (
            matching[0].net_quantity != position.net_quantity
            or matching[0].last_price != position.last_price
        ):
            return [], "POSITION_MARK_RECONCILIATION_MISMATCH"
        marks.append(
            {
                "position_id": position.id,
                "price": position.last_price,
                "quantity": position.net_quantity,
                "marked_at": _utc(position.marked_at),
            }
        )
    return marks, None
