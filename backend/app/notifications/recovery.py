"""Reconstruct missed PAPER closes from sealed fills, never today's positions."""

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST, UTC
from app.core.enums import Severity
from app.core.sessions import session_bounds
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Trade
from app.modes import TradingMode
from app.notifications.outbox import enqueue
from app.portfolio.fifo import Lot, match_fill


async def recorded_close(session, start, cutoff):
    seals = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.mode == TradingMode.PAPER,
                    AuditEvent.event_type == "PAPER_FILL_RECORDED",
                    AuditEvent.occurred_at <= cutoff,
                )
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ).all()
    )
    expected = set(
        await session.scalars(
            sa.select(Trade.id).where(Trade.mode == TradingMode.PAPER, Trade.executed_at <= cutoff)
        )
    )
    if any(not verify_records([seal]) for seal in seals):
        raise ValueError("historical fill audit integrity failure")
    fills = [seal.result for seal in seals]
    if {fill["fill_id"] for fill in fills} != expected:
        return None
    positions, sequences, symbols = {}, {}, {}
    gross, charges = Decimal(0), Decimal(0)
    daily, costed = [], True
    for fill in sorted(fills, key=lambda item: (item["position_id"], item["fifo_sequence"])):
        executed = datetime.fromisoformat(fill["executed_at"])
        if executed.utcoffset() is None or executed > cutoff:
            raise ValueError("historical fill observation time mismatch")
        position = fill["position_id"]
        sequence = sequences.get(position, 0) + 1
        if fill["fifo_sequence"] != sequence or fill["side"] not in {"BUY", "SELL"}:
            raise ValueError("historical fill sequence or direction mismatch")
        signed = fill["quantity"] * (1 if fill["side"] == "BUY" else -1)
        matched = match_fill(
            positions.get(position, ()), Lot(signed, Decimal(fill["price"]), fill["fill_id"])
        )
        positions[position], sequences[position] = matched.lots, sequence
        symbols[position] = fill["trading_symbol"]
        if executed >= start:
            daily.append(fill)
            gross += matched.realised
            charges += Decimal(fill["charges"])
            costed = costed and fill["cost_status"] == "ESTIMATED"
    return {
        "fill_count": len(daily),
        "traded_position_count": len({fill["position_id"] for fill in daily}),
        "fill_ids": [fill["fill_id"] for fill in daily],
        "gross_realised_pnl": gross,
        "charges": charges if costed else None,
        "net_realised_pnl": gross - charges if costed else None,
        "cost_status": "ESTIMATED"
        if costed and daily
        else ("NO_FILLS" if not daily else "UNAVAILABLE"),
        "open_positions": [
            {
                "id": identifier,
                "symbol": symbols[identifier],
                "quantity": sum(lot.quantity for lot in lots),
            }
            for identifier, lots in positions.items()
            if lots
        ],
        "position_history_available": True,
    }


class PaperSummaryRecovery:
    def __init__(self, worker):
        self.worker = worker

    async def catch_up(self):
        now = self.worker.clock.now().astimezone(IST)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with db_session.session_scope() as session:
            first_cycle = await session.scalar(
                sa.select(sa.func.min(AuditEvent.occurred_at)).where(
                    AuditEvent.mode == TradingMode.PAPER,
                    AuditEvent.event_type == "PAPER_SESSION_TRANSITION",
                    AuditEvent.occurred_at < today,
                )
            )
            first_fill = await session.scalar(
                sa.select(sa.func.min(Trade.executed_at)).where(
                    Trade.mode == TradingMode.PAPER,
                    Trade.executed_at < today,
                )
            )
            dates = [
                _utc(moment).astimezone(IST).date()
                for moment in (first_cycle, first_fill)
                if moment
            ]
            if not dates:
                return
            summaries = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.mode == TradingMode.PAPER,
                            AuditEvent.event_type.in_(
                                ["PAPER_DAILY_SUMMARY", "PAPER_DAILY_SUMMARY_CATCHUP"]
                            ),
                        )
                    )
                ).all()
            )
            if any(not verify_records([row]) for row in summaries):
                raise ValueError("historical summary index integrity failure")
            known = {row.chain_id for row in summaries}
        day, published = min(dates), 0
        while day < now.date() and published < 3:
            if self.worker.calendar.is_year_complete(day.year):
                bounds = session_bounds(day, calendar=self.worker.calendar)
                if bounds and f"paper-eod:{day}" not in known:
                    await self.publish(day, bounds[1])
                    published += 1
            day += timedelta(days=1)

    async def publish(self, day, close):
        if day >= self.worker.clock.now().astimezone(IST).date():
            raise ValueError("recovery requires a past session")
        bounds = (
            session_bounds(day, calendar=self.worker.calendar)
            if (self.worker.calendar.is_year_complete(day.year))
            else None
        )
        if bounds is None or bounds[1] != close:
            raise ValueError("recovery requires verified calendar bounds")
        identifier = f"paper-eod:{day}"
        cutoff = close.astimezone(UTC)
        start = close.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with db_session.session_scope() as session:
            existing = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == identifier)
                        .order_by(AuditEvent.sequence)
                        .with_for_update()
                    )
                ).all()
            )
            if not verify_records(existing):
                raise ValueError("historical summary audit integrity failure")
            if existing:
                return
            history = await recorded_close(session, start, cutoff)
            errors = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.mode == TradingMode.PAPER,
                            AuditEvent.severity == Severity.CRITICAL,
                            AuditEvent.occurred_at >= start,
                            AuditEvent.occurred_at <= cutoff,
                            ~AuditEvent.event_type.like("NOTIFICATION_%"),
                        )
                    )
                ).all()
            )
            for chain_id in {row.chain_id for row in errors}:
                chain = list(
                    (
                        await session.scalars(
                            sa.select(AuditEvent)
                            .where(AuditEvent.chain_id == chain_id)
                            .order_by(AuditEvent.sequence)
                        )
                    ).all()
                )
                if not verify_records(chain):
                    raise ValueError("historical error audit integrity failure")
            result = {
                "session_date": day,
                "session_close": close,
                "as_of": close,
                "mode": "PAPER",
                "execution_realism": "SIMULATED",
                "scope": "MISSED_SESSION_RECOVERY",
                "cycle_error": None,
                "worker_review_required": None,
                "reconciliation": "NOT_CONFIRMED",
                "fill_count": None,
                "traded_position_count": None,
                "fill_ids": [],
                "gross_realised_pnl": None,
                "charges": None,
                "net_realised_pnl": None,
                "cost_status": "UNAVAILABLE",
                "open_positions": [],
                "position_history_available": False,
                "order_history_available": False,
                "error_history_available": True,
                "unresolved_order_ids": [],
                "critical_event_ids": [row.id for row in errors],
                "valuation": {
                    "status": "UNAVAILABLE",
                    "reason": "HISTORICAL_MARKS_AND_RECONCILIATION_UNVERIFIED",
                },
                "limit_utilisation": {
                    "positions_used": len(history["open_positions"]) if history else None,
                    "max_concurrent_positions": None,
                    "risk_config_version": None,
                    "daily_loss": None,
                    "drawdown": None,
                    "exposure": None,
                    "unavailable_reason": "HISTORICAL_CONFIGURATION_AND_VALUATION_UNVERIFIED",
                },
                "reconstruction_status": "SEALED_FILLS_ONLY"
                if history
                else "CUTOFF_FILL_EVIDENCE_UNAVAILABLE",
            } | (history or {})
            event = await AuditService(self.worker.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=identifier,
                    event_type="PAPER_DAILY_SUMMARY_CATCHUP",
                    actor="paper_summary_recovery",
                    mode=TradingMode.PAPER,
                    severity="WARNING",
                ),
                {"result": result},
                expected_count=0,
            )
            await enqueue(
                session,
                key=identifier,
                event_type="DAILY_SUMMARY_CATCHUP",
                severity="WARNING",
                message=f"PAPER SIMULATED missed session {day}; {result['reconstruction_status']}; "
                f"recorded fills={result['fill_count'] if history else 'UNAVAILABLE'}; "
                f"gross={result['gross_realised_pnl'] if history else 'UNAVAILABLE'}; "
                "historical reconciliation, marks, limits and coverage UNVERIFIED. "
                f"audit={event.id}",
                clock=self.worker.clock,
                source_event=event,
            )
