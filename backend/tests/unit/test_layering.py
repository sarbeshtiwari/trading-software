"""Architectural rules enforced mechanically.

Covers ARCH-010, ARCH-012 (no ad-hoc clocks), ARCH-014 (no floats in money),
SEC-001 (no os.getenv outside config), TEST-026 (production never imports tests).

These are the rules that decay fastest under time pressure, so they are asserted
rather than documented.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

from tests.conftest import BACKEND_ROOT

pytestmark = pytest.mark.unit

APP_ROOT = BACKEND_ROOT / "app"

#: Modules allowed to break a given rule, because they *are* the abstraction.
CLOCK_EXEMPT = {"app/core/clock.py"}
ENV_EXEMPT = {"app/config.py"}
MONEY_MODULES = (
    "app/db/models",
    "app/brokers/models.py",
    "app/marketdata/models.py",
)


def _python_files() -> Iterator[Path]:
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _relative(path: Path) -> str:
    return path.relative_to(BACKEND_ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attribute_chain(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_layering_rules() -> None:
    """Strategies and agents may not reach the broker or execution layers.

    The decision path must hand a *proposal* to the risk engine, never place an
    order itself (AID-007). Enforced by import graph rather than by convention.
    """
    forbidden_importers = ("app/strategies", "app/agents", "app/llm", "app/analysis")
    forbidden_targets = ("app.brokers", "app.execution", "app.emergency")

    violations: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if not relative.startswith(forbidden_importers):
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
            elif isinstance(node, ast.Import):
                target = node.names[0].name
            else:
                continue
            if target.startswith(forbidden_targets):
                violations.append(f"{relative} imports {target}")

    assert not violations, "layering violations: " + "; ".join(violations)


def test_no_direct_environment_access_outside_config() -> None:
    """ARCH-009: one place knows how to read configuration."""
    violations: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if relative in ENV_EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Call):
                name = _attribute_chain(node.func)
                if name in {"os.getenv", "os.environ.get"}:
                    violations.append(f"{relative}:{node.lineno} calls {name}")
            if isinstance(node, ast.Subscript):
                if _attribute_chain(node.value) == "os.environ":
                    violations.append(f"{relative}:{node.lineno} indexes os.environ")

    # main.py reads one non-config developer convenience (UVICORN_RELOAD); allow
    # exactly that and nothing else.
    violations = [v for v in violations if "app/main.py" not in v]
    assert not violations, "direct environment access: " + "; ".join(violations)


def test_no_ad_hoc_clocks() -> None:
    """ARCH-012: time comes from the injected clock, never from the module."""
    banned = {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "time.time",
    }
    violations: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if relative in CLOCK_EXEMPT:
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _attribute_chain(node.func)
            if name in banned or name.endswith(("datetime.now", "datetime.utcnow")):
                violations.append(f"{relative}:{node.lineno} calls {name}")

    assert not violations, "ad-hoc clock use: " + "; ".join(violations)


def test_no_floats_in_money_paths() -> None:
    """ARCH-014: money and prices are Decimal everywhere they are modelled."""
    violations: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if not relative.startswith(MONEY_MODULES):
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.AnnAssign):
                annotation = ast.unparse(node.annotation)
                if "float" in annotation:
                    target = ast.unparse(node.target)
                    violations.append(f"{relative}:{node.lineno} {target}: {annotation}")

    assert not violations, "float in a money path: " + "; ".join(violations)


def test_fixtures_not_in_production() -> None:
    """TEST-026: production code never imports test fixtures."""
    violations: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and (module == "tests" or module.startswith("tests.")):
                violations.append(f"{_relative(path)} imports {module}")

    assert not violations, "production imports tests: " + "; ".join(violations)


def test_test_only_helpers_are_not_called_by_production() -> None:
    """Escape hatches exist for tests; production must not use them."""
    banned = {
        "reset_mode_for_testing",
        "reset_health_registry",
        "reset_trading_gate",
        "reset_ulid_state_for_testing",
        "reset_lifecycle",
    }
    definers = {
        "app/modes.py",
        "app/monitoring/healthchecks.py",
        "app/monitoring/gate.py",
        "app/core/ids.py",
        "app/core/lifecycle.py",
    }

    violations: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        if relative in definers:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                name = _attribute_chain(node.func).split(".")[-1]
                if name in banned:
                    violations.append(f"{relative}:{node.lineno} calls {name}")

    assert not violations, "test helper used in production: " + "; ".join(violations)


def test_every_module_is_importable() -> None:
    """A module that does not import is a module nobody has run."""
    import importlib

    failures: list[str] = []
    for path in _python_files():
        relative = _relative(path)
        module = relative[: -len(".py")].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            failures.append(f"{module}: {type(exc).__name__}: {exc}")

    assert not failures, "modules failed to import: " + "; ".join(failures)
