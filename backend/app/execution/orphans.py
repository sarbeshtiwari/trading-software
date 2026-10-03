"""Discover PAPER broker positions without inventing their trading history."""

import hashlib

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.data_origin import ExecutionRealism
from app.core.enums import PositionSide, PositionState
from app.core.errors import SafetyError
from app.db.models.instrument import Instrument
from app.db.models.trading import Order, Position
from app.execution import discrepancies
from app.modes import TradingMode
from app.notifications.outbox import enqueue


async def discover(session, remote_positions, clock):
    await discrepancies.system_row(session)
    positions = list(await session.scalars(sa.select(Position).where(
        Position.mode == TradingMode.PAPER, Position.net_quantity != 0,
    )))
    known = set()
    for position in positions:
        instrument = await session.get(Instrument, position.instrument_id)
        if instrument is None:
            raise SafetyError("POSITION_INSTRUMENT_UNAVAILABLE")
        known.add((
            instrument.exchange, position.trading_symbol, position.segment, position.product
        ))
    pending = list(await session.scalars(sa.select(Order).where(
        Order.mode == TradingMode.PAPER,
    )))
    for order in pending:
        if not order.status.is_terminal:
            known.add((order.exchange, order.trading_symbol, order.segment, order.product))
    evidence, adopted = [], []
    seen = set()
    for remote in remote_positions:
        key = (remote.exchange, remote.trading_symbol, remote.segment, remote.product)
        if key in seen:
            raise SafetyError("AMBIGUOUS_BROKER_POSITION_SNAPSHOT")
        seen.add(key)
        if not remote.net_quantity or key in known:
            continue
        evidence.append({
            "exchange": remote.exchange, "symbol": remote.trading_symbol,
            "segment": remote.segment, "product": remote.product,
            "quantity": remote.net_quantity, "average_price": remote.average_price,
            "accounting": "UNAVAILABLE_HISTORY_NOT_RECONSTRUCTED",
        })
        instruments = list(await session.scalars(sa.select(Instrument).where(
            Instrument.exchange == remote.exchange,
            Instrument.segment == remote.segment,
            Instrument.trading_symbol == remote.trading_symbol,
        )))
        if len(instruments) != 1:
            evidence[-1]["instrument_mapping"] = "UNAVAILABLE_OR_AMBIGUOUS"
            continue
        if not remote.average_price.is_finite() or remote.average_price <= 0:
            raise SafetyError("INVALID_ORPHAN_COST_BASIS")
        identity = "adopted:PAPER:" + "|".join(
            value.value if hasattr(value, "value") else value for value in key
        )
        identifier = "pos_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if await session.get(Position, identifier) is not None:
            raise SafetyError("PREVIOUS_ORPHAN_HISTORY_REQUIRES_REVIEW")
        position = Position(
            id=identifier, instrument_id=instruments[0].id,
            trading_symbol=remote.trading_symbol, segment=remote.segment, product=remote.product,
            side=PositionSide.LONG if remote.net_quantity > 0 else PositionSide.SHORT,
            state=PositionState.ADOPTED, net_quantity=remote.net_quantity,
            average_price=remote.average_price, bought_quantity=remote.bought_quantity,
            sold_quantity=remote.sold_quantity, realised_pnl=sa.null(),
            unrealised_pnl=sa.null(), total_charges=sa.null(),
            is_protected=False, adopted_from_broker=True,
            mode=TradingMode.PAPER, execution_realism=ExecutionRealism.SIMULATED,
        )
        session.add(position)
        await session.flush()
        record = await AuditService(clock).append_in_session(
            session, AuditIdentity(
                chain_id=identifier, event_type="PAPER_ORPHAN_ADOPTED",
                actor="paper_reconciliation", mode=TradingMode.PAPER,
            ), {"position_id": identifier, "result": evidence[-1]},
        )
        await enqueue(
            session, key=f"orphan:{identifier}", event_type="PAPER_ORPHAN_ADOPTED",
            severity="CRITICAL", message=(
                f"PAPER orphan position {identifier} adopted for inspection. "
                "Protection and accounting unavailable; owner recovery required; entries blocked."
            ), clock=clock, source_event=record,
        )
        adopted.append(identifier)
    if evidence:
        await discrepancies.observe(
            session, local={"adopted_position_ids": adopted},
            broker={"mode": "PAPER", "orphan_positions": evidence},
            delta={"recovery_required": "ORPHAN_POSITION_HISTORY_AND_PROTECTION"}, clock=clock,
        )
    return bool(evidence)
