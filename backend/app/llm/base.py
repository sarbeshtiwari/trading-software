"""Advisory review has no prices, quantity, confidence or control authority."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from app.analysis.equity import EvidenceModel


class AdvisoryReview(EvidenceModel):
    action: Literal["CONTINUE", "ABSTAIN"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProviderReply:
    raw: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    outcome: str = "SUCCESS"


class ProviderFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LLMProvider(ABC):
    @abstractmethod
    async def review(self, inputs, *, repair=False, task=None) -> ProviderReply:
        raise NotImplementedError


class LLMSchemaError(ProviderFailure):
    def __init__(self):
        super().__init__("SCHEMA_ERROR")
