"""Opaque proposal receipts bind audited model output to one decision context."""

from hashlib import sha256

import sqlalchemy as sa
from pydantic import Field

from app.agents.tasks import PROPOSAL_AGENT, validate_proposal_agent
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.snapshots import canonical, freeze_snapshot
from app.core.clock import UTC
from app.db.models.audit import AuditEvent
from app.db.models.llm import LLMCall
from app.llm.fallback import ProposalInputs
from app.llm.schema import decode_json


class ReceiptProposal(EvidenceModel):
    call_id: str = Field(min_length=1, max_length=40)


class ReceiptError(Exception):
    def __init__(self, code="LLM_RECEIPT_INVALID"):
        self.code = code
        super().__init__(code)


def context_digest(context):
    return sha256(
        canonical(
            freeze_snapshot(context.model_dump(mode="json", exclude={"llm_available"}))
        ).encode()
    ).hexdigest()


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def resolve_receipt(
    session, identifier, context, *, clock, linked_to=None, evidence_snapshots=None
):
    call = await session.get(LLMCall, identifier, populate_existing=True)
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == identifier,
                )
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    try:
        if call is None or len(events) != 2 or not verify_records(events):
            raise ValueError("missing or broken receipt audit")
        request, result = events
        if (
            call.provider != "claude"
            or call.outcome != "SUCCESS"
            or call.schema_valid is not True
            or call.purpose != PROPOSAL_AGENT.name
            or call.schema_name != PROPOSAL_AGENT.schema_name
            or call.prompt_version != PROPOSAL_AGENT.version
            or call.proposal_id != linked_to
            or request.event_type != "ADVISORY_REQUEST"
            or result.event_type != "ADVISORY_PROPOSAL"
            or request.mode != context.market.mode
            or result.mode != context.market.mode
            or call.correlation_id != context.cycle_id
            or request.correlation_id != context.cycle_id
            or result.correlation_id != context.cycle_id
            or not _utc(request.occurred_at) <= _utc(result.occurred_at) <= clock.utcnow()
        ):
            raise ValueError("receipt identity or state mismatch")
        archive = call.request_context
        if (
            request.data_used != archive
            or archive["decision_binding"] != context_digest(context)
            or archive["task"] != PROPOSAL_AGENT.model_dump(mode="json")
            or archive["request"]["model"] != call.model_id
            or sha256(canonical(archive["request"]).encode()).hexdigest() != call.prompt_hash
            or result.result["outcome"] != "SUCCESS"
            or result.result["review"] != call.parsed_response
            or sha256(call.raw_response.encode()).hexdigest() != result.result["raw_response_hash"]
        ):
            raise ValueError("receipt contents disagree with sealed audit")
        inputs = ProposalInputs.model_validate(archive["inputs"])
        calendar = inputs.event_control
        if calendar is not None and (
            calendar.instrument_id != context.market.instrument_id
            or calendar.origin != context.market.data_origin
            or calendar.as_of > _utc(request.occurred_at)
            or calendar.known_at is None
            or calendar.known_at > calendar.as_of
            or calendar.event_id is None
        ):
            raise ValueError("receipt calendar identity or knowledge mismatch")
        signal = inputs.signal
        if (
            signal.instrument_id != context.market.instrument_id
            or signal.strategy_id != context.strategy.id
            or signal.strategy_version != context.strategy.version
            or signal.product != context.strategy.product
            or signal.timeframe_seconds != context.strategy.timeframe_seconds
            or signal.generated_at != context.market.as_of
            or signal.data_origin != context.market.data_origin
            or signal.instrument_key not in context.strategy.universe
        ):
            raise ValueError("receipt signal disagrees with decision context")
        if evidence_snapshots is not None and inputs.grounded_evidence != evidence_snapshots:
            raise ValueError("source evidence changed since advisory request")
        parsed = validate_proposal_agent(decode_json(call.raw_response), inputs)
        if parsed.model_dump(mode="json") != call.parsed_response or parsed.proposal is None:
            raise ValueError("receipt does not contain a proposal")
        return parsed.proposal
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise ReceiptError() from error
