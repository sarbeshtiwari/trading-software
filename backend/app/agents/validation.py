"""Deterministic proposal validation against persisted point-in-time evidence."""

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field, ValidationError, model_validator

from app.agents.proposal import TradeProposal
from app.analysis.equity import EquitySnapshot, EvidenceModel
from app.audit.service import AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.clock import IST, UTC, Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import InstrumentType, SignalDirection
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.equity import EquityEvidence
from app.db.models.fundamental_versions import FundamentalVersion
from app.db.models.instrument import Instrument
from app.db.models.news import NewsItem
from app.news.research import admitted_news
from app.news.verification import verified_news

logger = get_logger("agents.validation")


class ValidationPolicy(EvidenceModel):
    confidence_floor: Decimal = Field(ge=0, le=1)
    max_entry_deviation_pct: Decimal = Field(gt=0)
    min_stop_atr_multiple: Decimal = Field(gt=0)
    max_stop_atr_multiple: Decimal = Field(gt=0)
    max_market_age_seconds: int = Field(gt=0, strict=True)
    max_evidence_age_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def bounds(self):
        if self.min_stop_atr_multiple > self.max_stop_atr_multiple:
            raise ValueError("invalid ATR bounds")
        return self


class ValidationMarket(EvidenceModel):
    instrument_id: str
    as_of: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    data_origin: DataOrigin
    price: Decimal = Field(gt=0)
    atr: Decimal = Field(gt=0)


class ValidationResult(EvidenceModel):
    valid: bool
    code: str
    proposal: TradeProposal | None
    resolved_sources: tuple[str, ...] = ()
    evidence_snapshots: dict[str, dict] = Field(default_factory=dict)


def parse_proposal(payload: str | dict) -> ValidationResult:
    try:
        proposal = (
            TradeProposal.model_validate_json(payload)
            if isinstance(payload, str)
            else TradeProposal.model_validate(payload)
        )
        return ValidationResult(valid=True, code="SCHEMA_VALID", proposal=proposal)
    except ValidationError as error:
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        tampered = any(item["type"] == "extra_forbidden" for item in errors)
        field_codes = {
            "direction": "INVALID_DIRECTION",
            "entry": "INVALID_PRICE",
            "stop_loss": "INVALID_PRICE",
            "target": "INVALID_PRICE",
            "quantity": "INVALID_QUANTITY",
            "confidence": "INVALID_CONFIDENCE",
        }
        field = errors[0]["loc"][0] if errors[0]["loc"] else None
        code = (
            "CONTROL_TAMPER_ATTEMPT"
            if tampered
            else field_codes.get(field, "INVALID_PROPOSAL_SCHEMA")
        )
        logger.warning("Proposal rejected", extra={"reason": code})
        return ValidationResult(valid=False, code=code, proposal=None)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProposalValidator:
    def __init__(self, policy: ValidationPolicy, clock: Clock | None = None):
        self.policy = policy
        self.clock = clock or get_clock()

    async def validate(self, payload: str | dict, market: ValidationMarket) -> ValidationResult:
        parsed = parse_proposal(payload)
        if not parsed.valid:
            return parsed
        proposal = parsed.proposal
        market = ValidationMarket.model_validate(market.model_dump())
        if (
            market.as_of > self.clock.now()
            or not market.observed_at <= market.available_at <= market.as_of
            or market.as_of - market.observed_at
            > timedelta(seconds=self.policy.max_market_age_seconds)
        ):
            return self._reject(proposal, "STALE_OR_FUTURE_MARKET")
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, proposal.instrument)
            code = self._instrument_and_prices(proposal, instrument, market)
            if code:
                return self._reject(proposal, code)
            if not proposal.evidence or len(
                {(item.kind, item.source_id) for item in proposal.evidence}
            ) != len(proposal.evidence):
                return self._reject(proposal, "INVALID_EVIDENCE_REFERENCES")
            resolved = []
            snapshots = {}
            for reference in proposal.evidence:
                try:
                    found = await self._resolve(session, reference, instrument, market)
                except (ValueError, TypeError, KeyError):
                    found = False
                if not found:
                    return self._reject(proposal, "EVIDENCE_UNRESOLVABLE")
                resolved.append(reference.source_id)
                snapshots[f"{reference.kind}:{reference.source_id}"] = freeze_snapshot(found)
        return ValidationResult(
            valid=True,
            code="VALIDATED",
            proposal=proposal,
            resolved_sources=tuple(resolved),
            evidence_snapshots=snapshots,
        )

    def _reject(self, proposal, code):
        logger.info("Proposal rejected", extra={"reason": code})
        return ValidationResult(valid=False, code=code, proposal=proposal)

    def _instrument_and_prices(self, proposal, instrument, market):
        if instrument is None:
            return "INSTRUMENT_UNAVAILABLE"
        sign = 1 if proposal.direction == SignalDirection.LONG else -1
        stop_distance = sign * (proposal.entry - proposal.stop_loss)
        checks = (
            ("INSTRUMENT_HISTORY_UNAVAILABLE", _utc(instrument.updated_at) > market.as_of),
            (
                "INSTRUMENT_NOT_TRADABLE",
                not instrument.is_active
                or instrument.is_restricted
                or instrument.instrument_type == InstrumentType.INDEX
                or (instrument.instrument_type.is_derivative and instrument.expiry_date is None),
            ),
            (
                "INSTRUMENT_EXPIRED",
                instrument.expiry_date is not None
                and instrument.expiry_date < market.as_of.astimezone(IST).date(),
            ),
            ("INSTRUMENT_MISMATCH", market.instrument_id != instrument.id),
            (
                "INVALID_TICK",
                any(
                    value % instrument.tick_size != 0
                    for value in (proposal.entry, proposal.stop_loss, proposal.target)
                ),
            ),
            ("INVALID_STOP_SIDE", stop_distance <= 0),
            ("INVALID_TARGET_SIDE", sign * (proposal.target - proposal.entry) <= 0),
            ("INVALID_LOT_QUANTITY", proposal.quantity % instrument.lot_size != 0),
            (
                "UNREALISTIC_ENTRY",
                abs(proposal.entry - market.price) / market.price * 100
                > self.policy.max_entry_deviation_pct,
            ),
            (
                "INVALID_STOP_ATR",
                not self.policy.min_stop_atr_multiple
                <= stop_distance / market.atr
                <= self.policy.max_stop_atr_multiple,
            ),
            ("CONFIDENCE_BELOW_FLOOR", proposal.confidence < self.policy.confidence_floor),
        )
        return next((code for code, failed in checks if failed), None)

    async def _resolve_market(self, session, reference, instrument, market):
        cutoff = market.as_of.astimezone(UTC)
        oldest = cutoff - timedelta(seconds=self.policy.max_evidence_age_seconds)
        row = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.chain_id == reference.source_id,
                AuditEvent.event_type == "REFERENCE_MARKET_SNAPSHOT",
                AuditEvent.instrument_id == instrument.id,
                AuditEvent.occurred_at <= cutoff,
                AuditEvent.occurred_at >= oldest,
            )
        )
        if row is None or not await AuditService(self.clock).verify(
            reference.source_id, expected_count=1
        ):
            return False
        snapshot = row.data_used
        observed = datetime.fromisoformat(snapshot["quote"]["observed_at"])
        available = datetime.fromisoformat(snapshot["as_of"])
        if (
            snapshot["data_origin"] != market.data_origin.value
            or not oldest <= observed <= available <= cutoff
            or Decimal(snapshot["quote"]["ltp"]) != market.price
        ):
            return False
        return {"source_id": row.chain_id, "payload": snapshot}

    async def _resolve(self, session, reference, instrument, market):
        cutoff = market.as_of.astimezone(UTC)
        oldest = cutoff - timedelta(seconds=self.policy.max_evidence_age_seconds)
        if reference.kind == "MARKET":
            return await self._resolve_market(session, reference, instrument, market)
        if reference.kind == "EQUITY":
            row = await session.scalar(
                sa.select(EquityEvidence).where(
                    EquityEvidence.id == reference.source_id,
                    EquityEvidence.key
                    == f"{instrument.exchange.value}:{instrument.trading_symbol}",
                    EquityEvidence.origin == market.data_origin.value,
                    EquityEvidence.known_at <= cutoff,
                    EquityEvidence.known_at >= oldest,
                    EquityEvidence.received_at <= cutoff,
                )
            )
            if row is None:
                return False
            snapshot = EquitySnapshot.model_validate(row.payload)
            snapshot.require_as_of(
                market.as_of, timedelta(seconds=self.policy.max_evidence_age_seconds)
            )
            return {
                "source_id": row.id,
                "known_at": _utc(row.known_at),
                "received_at": _utc(row.received_at),
                "payload": snapshot.model_dump(mode="json"),
            }
        if reference.kind == "FUNDAMENTAL":
            row = await session.scalar(
                sa.select(FundamentalVersion).where(
                    FundamentalVersion.id == reference.source_id,
                    FundamentalVersion.instrument_id == instrument.id,
                    FundamentalVersion.known_at <= cutoff,
                    FundamentalVersion.known_at >= oldest,
                    FundamentalVersion.received_at <= cutoff,
                )
            )
            return (
                {
                    "source_id": row.id,
                    "known_at": _utc(row.known_at),
                    "received_at": _utc(row.received_at),
                    "source": row.source,
                    "payload": row.payload,
                }
                if row is not None
                else False
            )
        return await self._resolve_news(session, reference, instrument, market)

    async def _resolve_news(self, session, reference, instrument, market):
        cutoff = market.as_of.astimezone(UTC)
        oldest = cutoff - timedelta(seconds=self.policy.max_evidence_age_seconds)
        if reference.source_id.startswith("nra_"):
            return await admitted_news(
                session,
                reference.source_id,
                instrument_id=instrument.id,
                as_of=market.as_of,
                max_age=timedelta(seconds=self.policy.max_evidence_age_seconds),
                origin=market.data_origin,
            )
        row = await session.scalar(
            sa.select(NewsItem).where(
                NewsItem.id == reference.source_id,
                NewsItem.published_at <= cutoff,
                NewsItem.published_at >= oldest,
                NewsItem.fetched_at <= cutoff,
                NewsItem.created_at <= cutoff,
                NewsItem.updated_at <= cutoff,
            )
        )
        actionable = (
            row is not None
            and row.is_actionable
            and not row.conflicts_with
            and isinstance(row.entities, list)
            and any(
                isinstance(entity, dict) and entity.get("instrument_id") == instrument.id
                for entity in row.entities
            )
        )
        if not actionable:
            return False
        return await verified_news(
            session,
            row,
            as_of=market.as_of,
            max_age=timedelta(seconds=self.policy.max_evidence_age_seconds),
            origin=market.data_origin,
        )
