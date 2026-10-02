"""Dated NSE ban reports: retained source documents, audited admission and entry vetoes."""

import csv
import re
from datetime import date
from hashlib import sha256
from io import StringIO
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import IST, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, Segment
from app.core.errors import SafetyError
from app.db.models.instrument import Instrument
from app.modes import TradingMode
from app.news.entities import aware, history

SOURCE = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
MONTHS = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}


class BanReport(EvidenceModel):
    trade_date: date
    underlyings: tuple[str, ...]
    sha256: str


def parse_report(content: str) -> BanReport:
    if not content or len(content.encode("utf-8")) > 131072:
        raise ValueError("Ban report empty or oversized")
    lines = content.lstrip("\ufeff").splitlines()
    if not lines:
        raise ValueError("Ban report empty")
    header = re.fullmatch(
        r"Securities in Ban For Trade Date (\d{2})-([A-Z]{3})-(\d{4}):", lines[0].strip()
    )
    if header is None or header[2] not in MONTHS:
        raise ValueError("Unsupported or undated ban report")
    effective = date(int(header[3]), MONTHS[header[2]], int(header[1]))
    symbols = []
    try:
        rows = list(csv.reader(StringIO("\n".join(lines[1:])), strict=True))
    except csv.Error as error:
        raise ValueError("Malformed ban report CSV") from error
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if (
            len(row) != 2
            or row[0].strip() != str(len(symbols) + 1)
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9&._-]{0,63}", row[1].strip())
            or row[1].strip() in symbols
        ):
            raise ValueError("Malformed, duplicate or incomplete ban report rows")
        symbols.append(row[1].strip())
    return BanReport(
        trade_date=effective,
        underlyings=tuple(symbols),
        sha256=sha256(content.encode("utf-8")).hexdigest(),
    )


class BanAdmission(EvidenceModel):
    origin: DataOrigin
    source_url: Literal["https://nsearchives.nseindia.com/content/fo/fo_secban.csv"] = SOURCE
    known_at: AwareDatetime
    document: str = Field(min_length=1, max_length=131072)
    expected_event_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    confirmation: Literal["ADMIT PAPER NSE BAN REPORT"]

    @model_validator(mode="after")
    def valid_document(self):
        parse_report(self.document)
        if len(self.reason.strip()) < 10:
            raise ValueError("Ban report review reason required")
        return self


class BanState(EvidenceModel):
    exchange: Literal["NSE"] = "NSE"
    origin: DataOrigin
    as_of: AwareDatetime
    trade_date: date
    head_id: str | None = None
    event_id: str | None = None
    status: Literal["AVAILABLE", "UNAVAILABLE"] = "UNAVAILABLE"
    report: BanReport | None = None
    known_at: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    source_url: str | None = None
    verification: Literal["OWNER_ADMITTED_SOURCE_NOT_AUTOMATICALLY_VERIFIED"] = (
        "OWNER_ADMITTED_SOURCE_NOT_AUTOMATICALLY_VERIFIED"
    )


class BanEntryError(SafetyError):
    pass


def chain_id(origin):
    return f"nse-ban:PAPER:{DataOrigin(origin).value}"


async def state_at(session, origin, *, as_of=None):
    cutoff = as_of or get_clock().utcnow()
    if cutoff.utcoffset() is None or cutoff > get_clock().utcnow():
        raise ValueError("Nonfuture aware ban cutoff required")
    state = BanState(origin=origin, as_of=cutoff, trade_date=cutoff.astimezone(IST).date())
    records = await history(session, chain_id(origin), cutoff if as_of else None)
    previous = None
    for record in records:
        evidence = BanAdmission.model_validate(record.data_used)
        report = parse_report(evidence.document)
        if (
            record.event_type != "FNO_BAN_REPORT_ADMITTED"
            or record.mode != TradingMode.PAPER
            or evidence.origin != DataOrigin(origin)
            or evidence.expected_event_id != (previous.id if previous else None)
            or not evidence.known_at <= aware(record.occurred_at) <= cutoff
            or (previous and aware(previous.occurred_at) > aware(record.occurred_at))
            or record.result != report.model_dump(mode="json")
        ):
            raise ValueError("Ban report identity, content or chronology mismatch")
        previous = record
        state = state.model_copy(update={"head_id": record.id})
        if report.trade_date == state.trade_date:
            state = state.model_copy(
                update={
                    "event_id": record.id,
                    "status": "AVAILABLE",
                    "report": report,
                    "known_at": evidence.known_at,
                    "received_at": aware(record.occurred_at),
                    "source_url": evidence.source_url,
                }
            )
    return state


async def admit(session, evidence, *, actor):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise ValueError("Ban admission is PAPER-only")
    evidence = BanAdmission.model_validate(evidence.model_dump())
    before = await state_at(session, evidence.origin)
    if evidence.known_at > get_clock().utcnow() or evidence.expected_event_id != before.head_id:
        raise ValueError("Future source knowledge or stale ban review")
    records = await history(session, chain_id(evidence.origin))
    if (records[-1].id if records else None) != before.head_id:
        raise ValueError("Ban report changed during review")
    proposed = parse_report(evidence.document)
    for record in records:
        prior = BanAdmission.model_validate(record.data_used)
        prior_report = parse_report(prior.document)
        if prior_report.trade_date == proposed.trade_date and (
            evidence.known_at < prior.known_at
            or (evidence.known_at == prior.known_at and proposed.sha256 != prior_report.sha256)
        ):
            raise ValueError("Older or conflicting source revision")
    await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(evidence.origin),
            event_type="FNO_BAN_REPORT_ADMITTED",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "data_used": evidence.model_dump(mode="json"),
            "result": proposed.model_dump(mode="json"),
        },
        expected_count=len(records),
    )
    return await state_at(session, evidence.origin)


async def require_entries(session, market):
    if market.mode != TradingMode.PAPER:
        return
    instrument = await session.get(Instrument, market.instrument_id)
    if instrument is None or instrument.segment != Segment.FNO:
        return
    evidence = {
        "instrument_id": instrument.id,
        "underlying": instrument.underlying,
        "exchange": instrument.exchange.value,
    }
    if instrument.exchange != Exchange.NSE or not re.fullmatch(
        r"[A-Z0-9][A-Z0-9&._-]{0,63}", instrument.underlying or ""
    ):
        code = "FNO_BAN_SCOPE_UNAVAILABLE"
    else:
        current = await state_at(session, market.data_origin)
        evidence["ban_state"] = current.model_dump(mode="json")
        if current.status != "AVAILABLE":
            code = "FNO_BAN_REPORT_UNAVAILABLE"
        elif instrument.underlying in current.report.underlyings:
            code = "FNO_BAN_LISTED"
        else:
            return
    raise BanEntryError(code, code=code, context=evidence)
