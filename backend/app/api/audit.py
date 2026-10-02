"""Authenticated audit search and immutable-evidence drill-down."""

from datetime import date, datetime, time, timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query

from app.audit.read import AuditPage, AuditScopeTooLarge, AuditTrail, item, resolve, trail
from app.core.clock import IST, UTC, get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode

router = APIRouter(prefix="/audit", tags=["audit"])
Identifier = Annotated[str | None, Query(min_length=1, max_length=64)]


@router.get("/events", response_model=AuditPage)
async def audit_events(
    *,
    instrument_id: Identifier = None,
    decision_id: Identifier = None,
    trade_id: Identifier = None,
    day: date | None = None,
    mode: TradingMode = TradingMode.PAPER,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    statement = sa.select(AuditEvent).where(
        AuditEvent.mode == mode, AuditEvent.occurred_at <= get_clock().utcnow()
    )
    if instrument_id:
        statement = statement.where(AuditEvent.instrument_id == instrument_id)
    if day:
        if not 1900 <= day.year < 9999:
            raise HTTPException(422, "Audit day outside supported range")
        start = datetime.combine(day, time(), tzinfo=IST).astimezone(UTC)
        statement = statement.where(
            AuditEvent.occurred_at >= start, AuditEvent.occurred_at < start + timedelta(days=1)
        )
    async with db_session.session_scope() as session:
        for identifier in (decision_id, trade_id):
            if identifier:
                proposal, chain = await resolve(session, identifier, mode)
                statement = statement.where(
                    sa.or_(
                        AuditEvent.proposal_id == proposal if proposal else sa.false(),
                        AuditEvent.chain_id == chain if chain else sa.false(),
                    )
                )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
    return AuditPage(events=[item(row) for row in rows[:limit]], has_more=len(rows) > limit)


@router.get("/trails/{identifier}", response_model=AuditTrail)
async def audit_trail(identifier: str, mode: TradingMode = TradingMode.PAPER):
    if not 1 <= len(identifier) <= 64:
        raise HTTPException(422, "Invalid audit identifier")
    async with db_session.session_scope() as session:
        try:
            result = await trail(session, identifier, mode)
        except AuditScopeTooLarge as error:
            raise HTTPException(413, "Audit trail exceeds supported scope") from error
    if result is None:
        raise HTTPException(404, "Audit trail unavailable")
    return result
