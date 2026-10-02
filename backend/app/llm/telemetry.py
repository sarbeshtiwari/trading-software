"""Durable local-provider receipts; remote billing is not inferred from these rows."""

from hashlib import sha256
from time import perf_counter

from app.agents.proposal import TradeProposal
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import canonical, freeze_snapshot
from app.core.ids import new_id
from app.core.logging import redact_data
from app.db import session as db_session
from app.db.models.llm import LLMCall
from app.llm.fallback import DeterministicFallbackProvider, ProposalInputs


async def record_fallback(
    inputs: ProposalInputs, *, clock, mode, correlation_id, reason="QUANTITATIVE_MAPPING"
):
    provider = DeterministicFallbackProvider()
    started = perf_counter()
    proposal = provider.propose(inputs)
    raw = redact_data(proposal.model_dump_json())
    archived = TradeProposal.model_validate_json(raw)
    request = freeze_snapshot(inputs.model_dump(mode="json"))
    policy_hash = sha256(provider.policy.encode()).hexdigest()
    prompt_hash = sha256(
        canonical({"policy": provider.policy, "data": request}).encode()
    ).hexdigest()
    identifier = new_id("llm")
    async with db_session.session_scope() as session:
        session.add(
            LLMCall(
                id=identifier,
                provider=provider.provider,
                model_id=provider.model_id,
                purpose="quantitative_proposal",
                prompt_version=provider.version,
                prompt_hash=prompt_hash,
                input_tokens=None,
                output_tokens=None,
                cost_usd=0,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                outcome="FALLBACK_USED",
                repair_attempts=0,
                request_context={"policy_hash": policy_hash, "inputs": request, "reason": reason},
                raw_response=raw,
                parsed_response=archived.model_dump(mode="json"),
                schema_name="TradeProposal",
                schema_valid=True,
                containment_flags=[],
                correlation_id=correlation_id,
                called_at=clock.utcnow(),
            )
        )
        await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=identifier,
                event_type="ADVISORY_PROPOSAL",
                actor="deterministic_fallback",
                mode=mode,
            ),
            {
                "correlation_id": correlation_id,
                "instrument_id": inputs.signal.instrument_id,
                "strategy_id": inputs.signal.strategy_id,
                "data_used": {"request": request, "policy_hash": policy_hash},
                "result": {
                    "provider": provider.provider,
                    "version": provider.version,
                    "proposal": archived.model_dump(mode="json"),
                    "llm_available": False,
                    "reason": reason,
                },
            },
        )
    return identifier, proposal
