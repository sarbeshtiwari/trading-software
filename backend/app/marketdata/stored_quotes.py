"""Recover complete provider observations from their existing immutable audit records."""

import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import UTC
from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError, ValidationError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.marketdata.recorded import RecordedSnapshot, validate_snapshot
from app.marketdata.recordings import QuoteRecord
from app.modes import TradingMode


async def stored_quote(instrument, *, as_of, origin=DataOrigin.LIVE):
    if as_of.utcoffset() is None:
        raise SafetyError("STORED_QUOTE_CUTOFF_REQUIRES_TIMEZONE")
    as_of = as_of.astimezone(UTC)
    async with db_session.session_scope() as session:
        event = await session.scalar(
            sa.select(AuditEvent)
            .join(Instrument, Instrument.id == AuditEvent.instrument_id)
            .where(
                Instrument.trading_symbol == instrument.trading_symbol,
                Instrument.exchange == instrument.exchange,
                Instrument.segment == instrument.segment,
                AuditEvent.event_type.in_(
                    ["REFERENCE_QUOTE_SNAPSHOT", "REFERENCE_MARKET_SNAPSHOT"]
                ),
                AuditEvent.actor == "reference_ingestion",
                AuditEvent.mode == TradingMode.PAPER,
                AuditEvent.occurred_at <= as_of,
                AuditEvent.data_used["data_origin"].as_string() == origin.value,
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
    if event is None:
        return None
    if not await AuditService().verify(event.chain_id):
        raise SafetyError("STORED_QUOTE_INTEGRITY_FAILURE")
    try:
        record = QuoteRecord(
            kind="QUOTE",
            source=event.data_used["provider"],
            available_at=event.data_used["as_of"],
            value=event.data_used["quote"],
        )
        validate_snapshot(RecordedSnapshot(record.source, record.available_at, record.value))
        if (
            record.available_at > as_of
            or record.value.instrument != instrument
            or record.value.data_origin != origin
        ):
            raise ValueError("stored quote chronology or identity mismatch")
    except (ValueError, KeyError, TypeError, ValidationError) as error:
        raise SafetyError("STORED_QUOTE_INVALID") from error
    return record.value
