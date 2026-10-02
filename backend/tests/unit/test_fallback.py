"""Advisory mapping preserves numbers and rejects forged typed inputs."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.proposal import EvidenceReference
from app.llm.fallback import DeterministicFallbackProvider, ProposalInputs
from app.strategies.signal import Signal
from tests.unit.test_strategies import signal_data


def test_fallback_uses_only_declared_signal_and_never_sizes():
    inputs = ProposalInputs(
        signal=Signal(**signal_data()),
        lot_size=25,
        evidence=(EvidenceReference(kind="MARKET", source_id="fixture-market"),),
        exit_rules=("close on declared invalidation",),
    )
    proposal = DeterministicFallbackProvider().propose(inputs)
    assert (proposal.entry, proposal.stop_loss, proposal.target) == (
        Decimal(100), Decimal(99), Decimal(102)
    )
    assert proposal.quantity == 25
    assert proposal.confidence == 1
    assert proposal.evidence == inputs.evidence
    assert proposal.thesis == "test-price"
    assert "risk_limits" not in proposal.model_dump()
    forged = inputs.model_copy(update={"lot_size": 0})
    with pytest.raises(ValidationError):
        DeterministicFallbackProvider().propose(forged)
    forged = inputs.model_copy(update={"signal": inputs.signal.model_copy(update={"stop": 101})})
    with pytest.raises(ValidationError):
        DeterministicFallbackProvider().propose(forged)
