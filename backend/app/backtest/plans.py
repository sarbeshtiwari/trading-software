"""Explicit owner plans; HTTP clients select an identity, never a filesystem path."""

from pathlib import Path

from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.backtest.bootstrap import HistoricalManifest
from app.backtest.child import read_object
from app.backtest.windows import WalkForwardPlan
from app.marketdata.recordings import read_recording


class HistoricalPlan(EvidenceModel):
    id: str = Field(pattern=r"^[a-z0-9]{8,26}$")
    label: str = Field(min_length=1, max_length=100)
    manifest: str
    recording: str
    settings: str
    timeout_seconds: int = Field(default=3600, ge=1, le=21600)


class PlanRegistry(EvidenceModel):
    plans: tuple[HistoricalPlan, ...] = Field(max_length=50)
    experiments: tuple[WalkForwardPlan, ...] = Field(default=(), max_length=10)


def load_registry(path):
    if path is None:
        return ()
    registry = PlanRegistry.model_validate(read_object(path, 65536))
    if len({plan.id for plan in registry.plans}) != len(registry.plans):
        raise ValueError("duplicate historical plan identity")
    return registry.plans


def load_experiments(path):
    if path is None:
        return ()
    registry = PlanRegistry.model_validate(read_object(path, 65536))
    identifiers = [item.id for item in registry.plans] + [item.id for item in registry.experiments]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate plan or experiment identity")
    known = {item.id for item in registry.plans}
    if any(
        identifier not in known
        for experiment in registry.experiments
        for group in experiment.candidates
        for pair in group
        for identifier in (pair.train_plan, pair.test_plan)
    ):
        raise ValueError("experiment references unknown owner plan")
    return registry.experiments


def resolve_input(root, name):
    relative = Path(name)
    if relative.is_absolute():
        raise ValueError("historical plan paths must be relative")
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("historical plan path escapes owner directory")
    return target


def read_plan(registry_path, identifier):
    plan = next((item for item in load_registry(registry_path) if item.id == identifier), None)
    if plan is None:
        raise ValueError("unknown historical plan")
    root = Path(registry_path).resolve(strict=True).parent
    manifest = HistoricalManifest.model_validate(
        read_object(resolve_input(root, plan.manifest), 4 * 1024 * 1024)
    )
    recording = read_recording(resolve_input(root, plan.recording))
    settings = read_object(resolve_input(root, plan.settings), 65536)
    if manifest.run_id != identifier or manifest.recording_sha256 != recording.content_hash():
        raise ValueError("historical plan identity or recording mismatch")
    return plan, manifest, recording, settings
