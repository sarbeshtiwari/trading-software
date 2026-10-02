"""Authenticated read-only, integrity-checked PAPER summary reports."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query

from app.agents.validation import _utc
from app.audit.integrity import verify_records
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.modes import TradingMode
from app.notifications.report_schema import PaperSummaryBody, PaperSummaryList, PaperSummaryView

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/paper/daily", response_model=PaperSummaryList)
async def paper_daily_summaries(limit: Annotated[int, Query(ge=1, le=100)] = 20):
    now = get_clock().utcnow()
    async with db_session.session_scope() as session:
        records = list(
            (
                await session.scalars(
                    sa.select(AuditEvent)
                    .where(
                        AuditEvent.mode == TradingMode.PAPER,
                        AuditEvent.event_type.in_(
                            [
                                "PAPER_DAILY_SUMMARY",
                                "PAPER_DAILY_SUMMARY_RECOVERY",
                                "PAPER_DAILY_SUMMARY_CATCHUP",
                            ]
                        ),
                        AuditEvent.occurred_at <= now,
                    )
                    .order_by(AuditEvent.occurred_at.desc(), AuditEvent.sequence.desc())
                    .limit(limit)
                )
            ).all()
        )
        for chain_id in {row.chain_id for row in records}:
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
                raise HTTPException(409, "PAPER_SUMMARY_INTEGRITY_FAILURE")
    reports = []
    for record in records:
        try:
            body = PaperSummaryBody.model_validate(record.result)
            if not body.session_close <= body.as_of <= _utc(record.occurred_at) <= now:
                raise ValueError("summary observation time mismatch")
        except ValueError:
            raise HTTPException(409, "PAPER_SUMMARY_EVIDENCE_UNAVAILABLE") from None
        reports.append(
            PaperSummaryView(
                audit_id=record.id,
                chain_id=record.chain_id,
                sequence=record.sequence,
                event_type=record.event_type,
                recorded_at=_utc(record.occurred_at),
                summary=body,
            )
        )
    return PaperSummaryList(generated_at=now, reports=reports)
