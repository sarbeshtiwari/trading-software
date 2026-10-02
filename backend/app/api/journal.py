"""Authenticated journal inspection, annotation and bounded lossless exports."""

import csv
import hashlib
import io
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical
from app.config import get_settings
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.journal import JournalEntry
from app.journal import service
from app.journal.corrections import correct
from app.journal.schemas import (
    AnnotationRequest,
    AnnotationView,
    CorrectionRequest,
    JournalDetail,
    JournalList,
)
from app.modes import TradingMode

router = APIRouter(prefix="/journal", tags=["journal"])


class JournalFilters(EvidenceModel):
    mode: TradingMode | None = None
    kind: Literal["TRADE", "REJECTION", "ORDER_ACTION"] | None = None
    strategy_id: str | None = Field(default=None, max_length=64)
    instrument_id: str | None = Field(default=None, max_length=40)
    outcome: Literal["WIN", "LOSS", "FLAT", "UNAVAILABLE"] | None = None
    start: date | None = None
    end: date | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def ordered_dates(self):
        if any(value and not 1900 <= value.year < 9999 for value in (self.start, self.end)):
            raise ValueError("journal date filter out of supported range")
        if self.start and self.end and self.start > self.end:
            raise ValueError("journal start date must precede end date")
        return self

    def query(self):
        return service.selection(
            **(
                self.model_dump(exclude={"offset", "limit"})
                | {"mode": self.mode or get_settings().trading_mode}
            )
        )


async def require_entry(session, identifier):
    row = await session.get(JournalEntry, identifier)
    if row is None:
        raise HTTPException(404, "Journal entry unavailable")
    return row


@router.get("", response_model=JournalList)
async def list_journal(filters: Annotated[JournalFilters, Query()]):
    async with db_session.session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    filters.query().offset(filters.offset).limit(filters.limit + 1)
                )
            ).all()
        )
        try:
            entries = [await service.entry_view(session, row) for row in rows[: filters.limit]]
        except ValueError:
            raise HTTPException(409, "Journal integrity failure; inspect audit evidence") from None
        return JournalList(entries=entries, has_more=len(rows) > filters.limit)


@router.get("/export/{format}")
async def export_journal(
    format: Literal["json", "csv"], filters: Annotated[JournalFilters, Query()], request: Request
):
    if filters.offset != 0 or filters.limit != 50:
        raise HTTPException(422, "Export uses all matching records; omit pagination")
    async with db_session.session_scope() as session:
        rows = list((await session.scalars(filters.query().limit(2001))).all())
        if len(rows) > 2000:
            raise HTTPException(413, "Narrow journal filters to at most 2000 records")
        try:
            entries = [
                (await service.entry_view(session, row)).model_dump(mode="json") for row in rows
            ]
        except ValueError:
            raise HTTPException(409, "Journal integrity failure; export refused") from None
        if format == "json":
            payload = canonical({"entries": entries, "has_more": False, "encoding": "JSON"})
        else:
            output = io.StringIO(newline="")
            columns = [
                *JournalEntry.__table__.columns.keys(),
                "integrity",
                "annotations",
                "revision_root_id",
                "previous_version_id",
                "latest_version_id",
                "record_role",
            ]
            writer = csv.writer(output)
            writer.writerow(columns)
            writer.writerows([canonical(row[key]) for key in columns] for row in entries)
            payload = output.getvalue()
        encoded = payload.encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            raise HTTPException(413, "Narrow journal filters; export exceeds 16 MiB")
        await AuditService().append_in_session(
            session,
            AuditIdentity(
                chain_id=new_id("jex"),
                event_type="JOURNAL_EXPORTED",
                actor=request.state.principal.username,
                mode=filters.mode or get_settings().trading_mode,
            ),
            {
                "result": {
                    "format": format,
                    "count": len(entries),
                    "filters": filters.model_dump(mode="json"),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            },
        )
        return Response(
            encoded,
            media_type="application/json" if format == "json" else "text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="journal.{format}"',
                "Cache-Control": "no-store",
                "X-Journal-CSV-Cells": "JSON",
            },
        )


@router.get("/{identifier}", response_model=JournalDetail)
async def journal_detail(identifier: str):
    async with db_session.session_scope() as session:
        row = await require_entry(session, identifier)
        try:
            return await service.detail(session, row)
        except ValueError:
            raise HTTPException(409, "Journal lineage integrity failure") from None


@router.post("/{identifier}/annotations", response_model=AnnotationView, status_code=201)
async def annotation(identifier: str, body: AnnotationRequest, request: Request):
    async with db_session.session_scope() as session:
        row = await require_entry(session, identifier)
        try:
            return await service.annotate(session, row, body, request.state.principal.username)
        except ValueError:
            raise HTTPException(409, "Journal integrity failure; annotation refused") from None


@router.post("/{identifier}/corrections", response_model=JournalDetail, status_code=201)
async def correction(identifier: str, body: CorrectionRequest, request: Request):
    try:
        async with db_session.session_scope() as session:
            row = await require_entry(session, identifier)
            await service.entry_view(session, row)
            entry = await correct(session, row, body, request.state.principal.username)
            return await service.detail(session, entry)
    except (ValueError, IntegrityError):
        raise HTTPException(
            409, "Journal correction refused: integrity, version conflict or inapplicable change"
        ) from None
