"""Durable PAPER session-end observations, never inferred remote delivery."""

from decimal import Decimal

import sqlalchemy as sa

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import IST, UTC
from app.core.enums import OrderStatus, Severity
from app.core.sessions import session_bounds
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Order, Position, Trade
from app.modes import TradingMode
from app.notifications.order_hygiene import order_hygiene_snapshot
from app.notifications.outbox import enqueue
from app.notifications.valuation import session_valuation
from app.risk.active import active_limits


class PaperDailySummary:
    def __init__(self, worker):
        self.worker = worker

    async def publish_if_due(self, *, cycle_error=None):
        worker = self.worker
        now = worker.clock.now().astimezone(IST)
        if not worker.calendar.is_year_complete(now.year):
            return None
        bounds = session_bounds(now.date(), calendar=worker.calendar)
        if bounds is None or now < bounds[1]:
            return None
        identifier = f"paper-eod:{now.date()}"
        async with db_session.session_scope() as session:
            records = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == identifier)
                        .order_by(AuditEvent.sequence)
                        .with_for_update()
                    )
                ).all()
            )
            if not verify_records(records):
                raise ValueError("daily summary audit integrity failure")
            start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            cutoff = now.astimezone(UTC)
            trades = list(
                (
                    await session.scalars(
                        sa.select(Trade).where(
                            Trade.mode == TradingMode.PAPER,
                            Trade.executed_at >= start,
                            Trade.executed_at <= cutoff,
                        )
                    )
                ).all()
            )
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER, Position.net_quantity != 0
                        )
                    )
                ).all()
            )
            pending = list(
                (
                    await session.scalars(
                        sa.select(Order.id).where(
                            Order.mode == TradingMode.PAPER,
                            ~Order.status.in_(
                                [status for status in OrderStatus if status.is_terminal]
                            ),
                        )
                    )
                ).all()
            )
            errors = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent.id).where(
                            AuditEvent.mode == TradingMode.PAPER,
                            AuditEvent.occurred_at >= start,
                            AuditEvent.occurred_at <= cutoff,
                            AuditEvent.severity == Severity.CRITICAL,
                            ~AuditEvent.event_type.like("NOTIFICATION_%"),
                        )
                    )
                ).all()
            )
            gross = sum(
                (Decimal(trade.cost_breakdown["fifo"]["realised_delta"]) for trade in trades),
                Decimal(0),
            )
            costed = all(trade.cost_breakdown.get("status") == "ESTIMATED" for trade in trades)
            charges = sum((trade.total_charges for trade in trades), Decimal(0)) if costed else None
            limits = await active_limits(session)
            net_realised = gross - charges if charges is not None else None
            valuation = await session_valuation(
                session,
                worker,
                positions=positions,
                limits=limits,
                cutoff=cutoff,
                net_realised=net_realised,
                cycle_error=cycle_error,
            )
            result = {
                "session_date": now.date(),
                "session_close": bounds[1],
                "as_of": now,
                "mode": "PAPER",
                "execution_realism": "SIMULATED",
                "scope": "IST_DAY_TO_FIRST_POST_CLOSE_OBSERVATION",
                "cycle_error": cycle_error,
                "worker_review_required": worker.failed,
                "reconciliation": "NOT_CONFIRMED" if cycle_error else "COMPLETED_THIS_CYCLE",
                "fill_count": len(trades),
                "traded_position_count": len({trade.position_id for trade in trades}),
                "fill_ids": [trade.id for trade in trades],
                "gross_realised_pnl": gross,
                "charges": charges,
                "net_realised_pnl": net_realised,
                "valuation": valuation,
                "cost_status": "ESTIMATED"
                if trades and costed
                else ("NO_FILLS" if not trades else "UNAVAILABLE"),
                "open_positions": [
                    {
                        "id": position.id,
                        "symbol": position.trading_symbol,
                        "quantity": position.net_quantity,
                    }
                    for position in positions
                ],
                "unresolved_order_ids": pending,
                "order_hygiene": await order_hygiene_snapshot(session, start, cutoff),
                "critical_event_ids": errors,
                "limit_utilisation": {
                    "positions_used": len(positions),
                    "max_concurrent_positions": limits.max_concurrent_positions if limits else None,
                    "risk_config_version": limits.version if limits else None,
                    "daily_loss": valuation.get("daily_loss"),
                    "drawdown": valuation.get("drawdown"),
                    "exposure": valuation.get("exposure"),
                    "unavailable_reason": valuation["reason"],
                },
            }
            if records:
                previous = records[-1].result
                improved = (
                    (previous["reconciliation"] != "COMPLETED_THIS_CYCLE" and not cycle_error)
                    or (previous["open_positions"] and not positions)
                    or (previous["unresolved_order_ids"] and not pending)
                    or (previous["worker_review_required"] and not worker.failed)
                    or (
                        previous.get("valuation", {}).get("status") != "ESTIMATED"
                        and valuation["status"] == "ESTIMATED"
                    )
                )
                if not improved:
                    return previous
                result["previous_summary_id"] = records[-1].id
                result["scope"] = "POST_CLOSE_RECOVERY_OBSERVATION"
            severity = "WARNING" if positions or pending or cycle_error or worker.failed else "INFO"
            limit_text = "; ".join(
                f"{name}={valuation[name]['amount']}/{valuation[name]['limit']}"
                if valuation.get(name) is not None
                else f"{name}=UNAVAILABLE"
                for name in ("daily_loss", "drawdown", "exposure")
            )
            event = await AuditService(worker.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=identifier,
                    event_type="PAPER_DAILY_SUMMARY_RECOVERY" if records else "PAPER_DAILY_SUMMARY",
                    actor="paper_daily_summary",
                    mode=TradingMode.PAPER,
                    severity=severity,
                ),
                {"result": result},
                expected_count=len(records),
            )
            await enqueue(
                session,
                key=f"{identifier}:{event.id}" if records else identifier,
                event_type="DAILY_SUMMARY_RECOVERY" if records else "DAILY_SUMMARY",
                severity=severity,
                message=(
                    f"PAPER SIMULATED {now.date()}; fills={len(trades)}; "
                    f"gross realised={gross}; "
                    f"charges={charges if charges is not None else 'UNAVAILABLE'}; "
                    f"net realised={result['net_realised_pnl'] if costed else 'UNAVAILABLE'}; "
                    f"open positions={len(positions)}; pending orders={len(pending)}; "
                    f"critical audit events={len(errors)}; review required={worker.failed}; "
                    f"reconciliation={result['reconciliation']}; audit={event.id}. "
                    f"{limit_text}; "
                    f"scope={result['scope']}; not proof of full-session coverage."
                ),
                clock=worker.clock,
                source_event=event,
            )
            return event.result
