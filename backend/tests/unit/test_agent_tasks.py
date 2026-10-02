"""Agent declarations and grounding operate without execution or persistence capabilities."""

import ast
from pathlib import Path

import pytest

from app.agents.tasks import PROPOSAL_AGENT, AgentRegistry, AgentSpec, validate_proposal_agent
from app.llm.claude import ClaudeProvider
from app.llm.fallback import DeterministicFallbackProvider
from tests.unit.test_llm import inputs, settings


def output(**changes):
    proposal = DeterministicFallbackProvider().propose(inputs()).model_dump(mode="json")
    proposal.update({"thesis": "Price 100 is supplied evidence", **changes})
    return {"action": "CONTINUE", "proposal": proposal}


@pytest.mark.parametrize("field", list(AgentSpec.model_fields))
def test_agent_contract_requires_every_declaration(field):
    specification = PROPOSAL_AGENT.model_dump()
    del specification[field]
    with pytest.raises(ValueError):
        AgentRegistry().register(specification)


def test_versioning_and_stateless_request_reproduction():
    registry = AgentRegistry()
    registry.register(PROPOSAL_AGENT)
    changed = PROPOSAL_AGENT.model_copy(update={"prompt": "changed"})
    with pytest.raises(ValueError, match="version bump"):
        registry.register(changed)
    detached = registry.get(PROPOSAL_AGENT.name, PROPOSAL_AGENT.version)
    detached.output_schema["additionalProperties"] = True
    assert (
        registry.get(PROPOSAL_AGENT.name, PROPOSAL_AGENT.version).output_schema[
            "additionalProperties"
        ]
        is False
    )
    first = ClaudeProvider(settings()).request(inputs(), False, task=PROPOSAL_AGENT)
    second = ClaudeProvider(settings()).request(inputs(), False, task=PROPOSAL_AGENT)
    assert first == second
    assert first["system"] == PROPOSAL_AGENT.prompt
    assert first["output_config"]["format"]["schema"] == PROPOSAL_AGENT.output_schema


@pytest.mark.parametrize(
    "changes",
    [
        {"entry": "101"},
        {"confidence": "0.99"},
        {"thesis": "Price 999"},
        {"thesis": "Price 9.99e2"},
        {"thesis": "Price INR999"},
        {"thesis": "Price 9,999"},
        {"thesis": "Return .99"},
    ],
)
def test_agent_rejects_unprovided_numbers(changes):
    with pytest.raises(ValueError, match="UNGROUNDED_NUMBER"):
        validate_proposal_agent(output(**changes), inputs())


def test_agent_preserves_evidence_and_has_no_execution_authority():
    assert validate_proposal_agent(output(quantity=9999), inputs()).proposal.quantity == 9999
    with pytest.raises(ValueError, match="UNGROUNDED_PROPOSAL"):
        validate_proposal_agent(output(evidence=()), inputs())
    invalid = output()
    invalid["proposal"]["override_risk"] = True
    with pytest.raises(ValueError):
        validate_proposal_agent(invalid, inputs())
    source = Path(__file__).resolve().parents[2] / "app/agents/tasks.py"
    imports = [
        node.module
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        not name.startswith(("app.brokers", "app.db", "app.execution", "httpx")) for name in imports
    )
