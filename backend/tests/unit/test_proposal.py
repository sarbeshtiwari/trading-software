"""Strict advisory schema rejects control fields without logging external text."""

import ast

import pytest

from app.agents.proposal import TradeProposal
from app.agents.validation import parse_proposal
from tests.conftest import BACKEND_ROOT


def payload(**changes):
    values = {
        "instrument": "ins-test",
        "direction": "LONG",
        "strategy": "test-only",
        "entry": 100,
        "stop_loss": 98,
        "target": 104,
        "quantity": 100,
        "thesis": "Synthetic test only",
        "evidence": [{"kind": "FUNDAMENTAL", "source_id": "fund-test"}],
        "invalidation_conditions": ["Test condition"],
        "confidence": "0.8",
    }
    values.update(changes)
    return values


def test_proposal_schema():
    assert set(TradeProposal.model_fields) == {
        "instrument",
        "direction",
        "strategy",
        "entry",
        "stop_loss",
        "target",
        "quantity",
        "thesis",
        "evidence",
        "invalidation_conditions",
        "confidence",
    }
    assert parse_proposal(payload()).valid
    assert not parse_proposal("{invalid json").valid


@pytest.mark.parametrize(
    "change,code",
    [
        ({"direction": "BUY_NOW"}, "INVALID_DIRECTION"),
        ({"entry": 0}, "INVALID_PRICE"),
        ({"stop_loss": -1}, "INVALID_PRICE"),
        ({"target": "NaN"}, "INVALID_PRICE"),
        ({"quantity": 0}, "INVALID_QUANTITY"),
        ({"quantity": True}, "INVALID_QUANTITY"),
        ({"confidence": 2}, "INVALID_CONFIDENCE"),
        ({"invalidation_conditions": []}, "INVALID_PROPOSAL_SCHEMA"),
    ],
)
def test_invalid_proposal_schema(change, code):
    assert parse_proposal(payload(**change)).code == code


@pytest.mark.parametrize(
    "control", ["max_daily_loss", "override_risk", "trading_mode", "kill_switch", "account"]
)
def test_llm_cannot_alter_controls(control, caplog):
    result = parse_proposal(payload(**{control: "private-marker-not-for-logs"}))
    assert not result.valid and result.code == "CONTROL_TAMPER_ATTEMPT"
    assert "CONTROL_TAMPER_ATTEMPT" in str(
        [getattr(record, "reason", "") for record in caplog.records]
    )
    assert "private-marker-not-for-logs" not in caplog.text


def test_decision_engine_no_execution_path():
    root = BACKEND_ROOT / "app"
    modules = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(BACKEND_ROOT).with_suffix("")
        name = ".".join(relative.parts).removesuffix(".__init__")
        modules[name] = path
    pending = [name for name in modules if name.startswith("app.agents")]
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        assert not name.startswith(("app.execution", "app.brokers", "app.emergency"))
        visited.add(name)
        for node in ast.walk(ast.parse(modules[name].read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module, *(node.module + "." + alias.name for alias in node.names)]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                continue
            pending.extend(target for target in targets if target in modules)
