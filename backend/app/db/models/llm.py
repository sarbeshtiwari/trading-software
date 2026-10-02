"""LLM call accounting and archival — DB-016, LLM-008, LLM-012, AUDIT-005.

Every call to a language model is recorded: what was asked (by prompt version and
a redacted hash, never the raw prompt if it could contain secrets), what came
back, whether it validated, what it cost and how long it took.

Two reasons this table exists beyond cost tracking:

* **Audit.** A proposal must link to the exact model call that produced it.
* **Regression.** A stored raw response can be replayed through the validator
  after a schema change, which is how prompt changes get tested.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import JSONColumn, RATIO, Base, TimestampMixin

__all__ = ["LLMBudgetDay", "LLMCall", "LLMProviderState"]


class LLMCall(TimestampMixin, Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        sa.Index("ix_llm_calls_created", "created_at"),
        sa.Index("ix_llm_calls_purpose", "purpose"),
        sa.Index("ix_llm_calls_outcome", "outcome"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("llm"))

    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: Which agent/task made the call: ``news``, ``research``, ``scenario``, ...
    purpose: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    input_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(sa.Numeric(12, 6), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    #: SUCCESS | SCHEMA_ERROR | TIMEOUT | RATE_LIMITED | PROVIDER_ERROR
    #: | BUDGET_EXCEEDED | CIRCUIT_OPEN | FALLBACK_USED
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    error_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    repair_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    #: Structured inputs handed to the model (already sanitised and redacted).
    request_context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    #: Raw response text, retained for replay (LLM-012).
    raw_response: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    parsed_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    schema_name: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    schema_valid: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)

    temperature: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    max_output_tokens: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    #: Set when containment or grounding checks rejected part of the output.
    containment_flags: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)

    proposal_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)
    news_item_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    called_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class LLMBudgetDay(Base):
    __tablename__ = "llm_budget_days"
    day: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    reserved_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    spent_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    spent_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)


class LLMProviderState(Base):
    __tablename__ = "llm_provider_states"
    id: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    failures: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    open_until: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))
    lease_until: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))
    lease_call_id: Mapped[Optional[str]] = mapped_column(sa.String(40))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
