"""AID-001: exact owner-specified top-level proposal schema."""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.core.enums import SignalDirection


class EvidenceReference(EvidenceModel):
    kind: Literal["EQUITY", "NEWS", "FUNDAMENTAL", "MARKET"]
    source_id: str = Field(min_length=1)


class TradeProposal(EvidenceModel):
    instrument: str = Field(min_length=1)
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]
    strategy: str = Field(min_length=1)
    entry: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    target: Decimal = Field(gt=0)
    quantity: int = Field(gt=0, strict=True)
    thesis: str = Field(min_length=1)
    evidence: tuple[EvidenceReference, ...]
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
