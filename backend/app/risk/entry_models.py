"""Frozen evidence for auditable daily-count and loss-cooldown decisions."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.core.entry_windows import EntryWindow
from app.modes import TradingMode


class EntryPolicy(EvidenceModel):
    maximum_daily_entries: int = Field(gt=0, strict=True)
    loss_cooloff_minutes: int = Field(ge=0, strict=True)


class ClosedOutcome(EvidenceModel):
    journal_id: str
    closed_at: AwareDatetime
    net_pnl: Decimal | None


class EntryEvidence(EvidenceModel):
    window: EntryWindow | None = None
    version: Literal["ENTRY_POLICY_V1"] = "ENTRY_POLICY_V1"
    policy: EntryPolicy
    as_of: AwareDatetime
    mode: TradingMode
    data_origin: DataOrigin
    instrument_id: str
    counted_order_ids: tuple[str, ...]
    closed_outcomes: tuple[ClosedOutcome, ...]
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.counted_order_ids)) != len(self.counted_order_ids):
            raise ValueError("duplicate counted entry")
        if any(outcome.closed_at > self.as_of for outcome in self.closed_outcomes):
            raise ValueError("future closed outcome")
        return self
