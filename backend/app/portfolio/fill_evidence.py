"""Immutable economic fill evidence committed with PAPER position accounting."""

from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.db.models.event_outbox import RuntimeEventOutbox
from app.db.models.trading import Trade
from app.modes import TradingMode


def fill_snapshot(trade, trading_symbol):
    return {
        "fill_id": trade.id,
        "position_id": trade.position_id,
        "order_id": trade.order_id,
        "instrument_id": trade.instrument_id,
        "trading_symbol": trading_symbol,
        "side": trade.transaction_type.value,
        "quantity": trade.quantity,
        "price": trade.price,
        "executed_at": _utc(trade.executed_at),
        "charges": trade.total_charges,
        "cost_status": trade.cost_breakdown.get("status", "UNAVAILABLE"),
        "fifo_sequence": trade.cost_breakdown["fifo"]["sequence"],
    }


async def record_fill(session, identifier, trading_symbol, clock):
    trade = await session.get(Trade, identifier)
    if trade is None:
        raise ValueError("fill evidence requires a persisted fill")
    source = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=identifier,
            event_type="PAPER_FILL_RECORDED",
            actor="paper_execution",
            mode=TradingMode.PAPER,
        ),
        {
            "position_id": trade.position_id,
            "order_id": trade.order_id,
            "instrument_id": trade.instrument_id,
            "result": fill_snapshot(trade, trading_symbol),
        },
        expected_count=0,
    )
    session.add(RuntimeEventOutbox(audit_id=source.id))
