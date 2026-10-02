"""Isolated model boundary producing actual persisted application receipts."""

import json

import httpx

from app.agents.proposal import EvidenceReference
from app.agents.tasks import PROPOSAL_AGENT
from app.llm.claude import ClaudeProvider
from app.llm.fallback import DeterministicFallbackProvider, ProposalInputs
from app.llm.receipts import ReceiptProposal, context_digest
from app.llm.service import review_signal
from app.strategies.signal import Signal
from tests.integration.test_proposal import validator
from tests.unit.test_llm import response, settings
from tests.unit.test_strategies import signal_data


async def receipt(context, fake_clock, monkeypatch, *, signal_changes=None):
    evidence = (EvidenceReference(kind="FUNDAMENTAL", source_id="fund-test"),)
    data = ProposalInputs(
        signal=Signal(
            **(
                signal_data()
                | {
                    "stop": 98,
                    "targets": (104,),
                    "strategy_version": context.strategy.version,
                }
                | (signal_changes or {})
            )
        ),
        lot_size=1,
        evidence=evidence,
        exit_rules=context.strategy.exit_rules,
    )
    validated = await validator().validate(
        DeterministicFallbackProvider().propose(data).model_dump(),
        context.prices,
    )
    assert validated.valid
    data = data.model_copy(update={"grounded_evidence": validated.evidence_snapshots})
    body = DeterministicFallbackProvider().propose(data).model_copy(update={"quantity": 9999})
    monkeypatch.setattr(
        "app.llm.factory.ClaudeProvider",
        lambda config: ClaudeProvider(
            config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=response(
                        raw=json.dumps(
                            {
                                "action": "CONTINUE",
                                "proposal": body.model_dump(mode="json"),
                            }
                        )
                    ),
                )
            ),
        ),
    )
    identifier, result = await review_signal(
        data,
        settings(),
        clock=fake_clock,
        mode=context.market.mode,
        correlation_id=context.cycle_id,
        task=PROPOSAL_AGENT,
        decision_binding=context_digest(context),
    )
    assert result == ReceiptProposal(call_id=identifier)
    return result
