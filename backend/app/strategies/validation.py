"""SENT-005: validate declarative entry logic before strategy registration."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class InputKind(str, Enum):
    PRICE = "PRICE"
    TECHNICAL = "TECHNICAL"
    FUNDAMENTALS = "FUNDAMENTALS"
    SENTIMENT = "SENTIMENT"
    NEWS = "NEWS"


class EntryCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operator: Literal["LEAF", "AND", "OR", "NOT"]
    input: InputKind | None = None
    children: tuple["EntryCondition", ...] = ()

    @model_validator(mode="after")
    def shape(self):
        if self.operator == "LEAF":
            if self.input is None or self.children:
                raise ValueError("invalid leaf condition")
        elif self.input is not None or len(self.children) < (1 if self.operator == "NOT" else 2):
            raise ValueError("invalid logical condition")
        if self.operator == "NOT" and len(self.children) != 1:
            raise ValueError("NOT requires one child")
        return self


def validate_entry_condition(condition: EntryCondition) -> None:
    """Every satisfiable structural entry path must consult a non-sentiment input.

    This checks input dependencies, not the financial validity of predicates.
    The future strategy registry must invoke it; callers cannot use a declaration
    of unrelated required inputs to legitimise sentiment-only entry logic.
    """
    if _sentiment_only(condition):
        raise ValueError("SENTIMENT_ONLY_ENTRY")


def _sentiment_only(condition: EntryCondition, negated: bool = False) -> bool:
    if condition.operator == "LEAF":
        return condition.input in (InputKind.SENTIMENT, InputKind.NEWS)
    if condition.operator == "NOT":
        return _sentiment_only(condition.children[0], not negated)
    branches = [_sentiment_only(child, negated) for child in condition.children]
    conjunction = (condition.operator == "AND") != negated
    return all(branches) if conjunction else any(branches)
