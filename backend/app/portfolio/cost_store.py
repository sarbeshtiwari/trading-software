"""Audited tariff publication and point-in-time selection for PAPER orders."""

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.core.clock import get_clock
from app.core.enums import InstrumentType, Segment
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.modes import TradingMode
from app.portfolio.costs import FeeSchedule


class CostStore:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def publish(self, schedule: FeeSchedule, *, actor, reason):
        schedule = FeeSchedule.model_validate(schedule.model_dump())
        if schedule.known_at > self.clock.now():
            raise ValueError("cannot publish future fee knowledge")
        identifier = new_id("fee")
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=identifier, event_type="FEE_SCHEDULE", actor=actor, mode=TradingMode.PAPER
            ),
            {"data_used": schedule.model_dump(mode="json"), "result": {"reason": reason}},
        )
        return identifier

    async def for_order(self, request, as_of):
        async with db_session.session_scope() as session:
            if request.segment == Segment.FNO:
                instrument = await session.scalar(
                    sa.select(Instrument).where(
                        Instrument.exchange == request.exchange,
                        Instrument.segment == request.segment,
                        Instrument.trading_symbol == request.trading_symbol,
                    )
                )
                if instrument is None or instrument.instrument_type != InstrumentType.OPTION:
                    return None
            rows = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(
                            AuditEvent.event_type == "FEE_SCHEDULE",
                            AuditEvent.occurred_at <= as_of,
                        )
                        .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    )
                ).all()
            )
        for row in rows:
            if not await AuditService(self.clock).verify(row.chain_id, expected_count=1):
                raise ValueError("fee schedule audit integrity failure")
            schedule = FeeSchedule.model_validate(row.data_used)
            if (schedule.exchange, schedule.segment, schedule.product) != (
                request.exchange,
                request.segment,
                request.product,
            ):
                continue
            if (
                schedule.known_at <= as_of
                and schedule.effective_from <= as_of < schedule.effective_to
            ):
                return schedule
        return None
