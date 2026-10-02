"""Immutable strategy identities carried with research evidence, not approval."""

from typing import Literal

from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.strategies.base import StrategySpec
from app.strategies.registry import specification_hash


class StrategyBinding(EvidenceModel):
    specification: StrategySpec
    parameter_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verified(self):
        if specification_hash(self.specification) != self.parameter_hash:
            raise ValueError("strategy evidence parameter hash mismatch")
        return self


class BindingSummary(EvidenceModel):
    state: Literal["FIXED", "MIXED", "UNAVAILABLE"]
    parameter_hashes: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    strategy_versions: tuple[str, ...]


def summarize_bindings(values):
    bindings = [StrategyBinding.model_validate(value) for value in values if value is not None]
    hashes = tuple(sorted({item.parameter_hash for item in bindings}))
    return BindingSummary(
        state="UNAVAILABLE"
        if not values or len(bindings) != len(values)
        else "FIXED"
        if len(hashes) == 1
        else "MIXED",
        parameter_hashes=hashes,
        strategy_ids=tuple(sorted({item.specification.id for item in bindings})),
        strategy_versions=tuple(sorted({item.specification.version for item in bindings})),
    )
