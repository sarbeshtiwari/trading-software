"""Typed owner-facing journal records; unavailable values remain null."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.api.workspace import Row
from app.modes import TradingMode


class AnnotationRequest(EvidenceModel):
    note: str = Field(min_length=1, max_length=4000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def valid_text(self):
        if len(self.reason.strip()) < 10:
            raise ValueError("meaningful annotation reason required")
        if not self.note.strip() or any(not tag.strip() or len(tag) > 64 for tag in self.tags):
            raise ValueError("nonblank note and tags up to 64 characters required")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("duplicate annotation tags")
        return self


class AnnotationView(Row):
    id: str
    journal_entry_id: str
    note: str | None
    tags: list[str] | None
    author: str
    created_at: datetime
    updated_at: datetime


class EntryView(Row):
    id: str
    kind: str
    proposal_id: str | None
    risk_decision_id: str | None
    position_id: str | None
    entry_order_id: str | None
    exit_order_ids: list[str] | None
    audit_chain_id: str | None
    instrument_id: str | None
    trading_symbol: str | None
    strategy_id: str | None
    strategy_version: str | None
    direction: str | None
    planned_entry: Decimal | None
    actual_entry: Decimal | None
    planned_stop: Decimal | None
    planned_target: Decimal | None
    quantity: int | None
    risk_amount: Decimal | None
    opened_at: datetime | None
    actual_exit: Decimal | None
    exit_reason: str | None
    closed_at: datetime | None
    holding_period_seconds: int | None
    invalidation_fired: bool | None
    gross_pnl: Decimal | None
    charges: Decimal | None
    net_pnl: Decimal | None
    r_multiple: Decimal | None
    entry_slippage: Decimal | None
    exit_slippage: Decimal | None
    outcome: str | None
    regime_at_entry: str | None
    indicator_snapshot: dict[str, Any] | None
    chain_summary: dict[str, Any] | None
    news_used: list[Any] | None
    ai_thesis: str | None
    ai_confidence: Decimal | None
    plan_adherence: str | None
    rejection_code: str | None
    rejection_rule: str | None
    rejection_detail: str | None
    mode: TradingMode
    superseded_by: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    integrity: Literal["AUDIT_BOUND", "LEGACY_UNBOUND"]
    annotations: list[AnnotationView]
    revision_root_id: str
    previous_version_id: str | None
    latest_version_id: str
    record_role: Literal["ORIGINAL", "OWNER_CONTEXT_CORRECTION"]


class JournalList(EvidenceModel):
    entries: list[EntryView]
    has_more: bool
    scope: str = "Persisted records only; rejections and order actions are not closed trades."


class CorrectionRequest(EvidenceModel):
    expected_version: int = Field(ge=1, le=999)
    reason: str = Field(min_length=10, max_length=500)
    plan_adherence: Literal["FOLLOWED", "DEVIATED", "UNAVAILABLE"] | None = None
    rejection_detail: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def meaningful(self):
        if len(self.reason.strip()) < 10 or (
            self.rejection_detail is not None and not self.rejection_detail.strip()
        ):
            raise ValueError("meaningful correction reason and detail required")
        if self.plan_adherence is None and self.rejection_detail is None:
            raise ValueError("at least one contextual correction required")
        return self


class RevisionView(Row):
    entry_id: str
    previous_id: str
    root_id: str
    version: int
    actor: str
    reason: str
    changes: dict[str, Any]
    occurred_at: datetime


class JournalDetail(EvidenceModel):
    entry: EntryView
    proposal: dict[str, Any] | None
    candidate: dict[str, Any] | None
    risk_decisions: list[dict[str, Any]]
    sizing: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    order_events: list[dict[str, Any]]
    position: dict[str, Any] | None
    regime: dict[str, Any] | None
    audit: list[dict[str, Any]]
    gaps: list[str]
    lineage_status: Literal["AVAILABLE", "INCOMPLETE"]
    revisions: list[RevisionView] = Field(default_factory=list)
    correction_scope: str = (
        "Owner contextual assessment only; economic execution evidence is never editable."
    )
