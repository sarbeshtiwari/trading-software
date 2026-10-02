"""STRAT-001: mandatory strategy declarations; entries are proposals, never orders."""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.analysis.equity import EvidenceModel
from app.core.enums import MarketRegime, Product
from app.strategies.context import StrategyContext
from app.strategies.exits import ExitContext, ExitDecision, ExitPolicy, ExitState, evaluate_exit
from app.strategies.signal import Signal
from app.strategies.validation import EntryCondition, InputKind, validate_entry_condition

ContextInput = Literal[
    "candles", "indicators", "regime", "chain", "news", "sentiment", "fundamentals", "equity"
]


class StrategyRisk(EvidenceModel):
    requested_risk_fraction: Decimal = Field(gt=0, le=1)
    minimum_reward_risk: Decimal = Field(gt=0)


class StrategySpec(EvidenceModel):
    id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    timeframe_seconds: int = Field(gt=0, strict=True)
    universe: tuple[str, ...]
    permitted_regimes: tuple[MarketRegime, ...]
    required_inputs: tuple[ContextInput, ...]
    product: Product
    entry_condition: EntryCondition
    exit_rules: tuple[Literal["STOP", "TARGET", "TRAILING", "TIME", "INVALIDATION"], ...]
    risk: StrategyRisk
    exit_policy: ExitPolicy
    requires_llm: bool
    requires_event_calendar: bool
    max_input_age_seconds: int = Field(gt=0, strict=True)
    hypothesis: str = Field(min_length=1)
    validation_plan: str = Field(min_length=1)

    @field_validator("universe", "permitted_regimes", "required_inputs", "exit_rules")
    @classmethod
    def canonical(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate strategy declaration")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def contract(self):
        if not self.universe or not self.permitted_regimes or not self.required_inputs:
            raise ValueError("incomplete strategy declaration")
        if set(self.exit_rules) != {"STOP", "TARGET", "TRAILING", "TIME", "INVALIDATION"}:
            raise ValueError("all five exit paths must be declared")
        validate_entry_condition(self.entry_condition)
        referenced = _entry_inputs(self.entry_condition)
        if not referenced.issubset(self.required_inputs):
            raise ValueError("entry references undeclared context")
        return self


def _entry_inputs(condition):
    mapping = {
        InputKind.PRICE: "candles",
        InputKind.TECHNICAL: "indicators",
        InputKind.FUNDAMENTALS: "fundamentals",
        InputKind.NEWS: "news",
        InputKind.SENTIMENT: "sentiment",
    }
    if condition.input is not None:
        return {mapping[condition.input]}
    return set().union(*(_entry_inputs(child) for child in condition.children))


class Strategy(ABC):
    @property
    @abstractmethod
    def spec(self) -> StrategySpec: ...

    @abstractmethod
    def entry(self, context: StrategyContext) -> Signal | None: ...

    def exit(self, context: ExitContext, position: ExitState) -> ExitDecision:
        return evaluate_exit(
            position, context.mark, context.invalidation, self.spec.exit_policy, as_of=context.as_of
        )
