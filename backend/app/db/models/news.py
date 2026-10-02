"""News storage — DB-014, NEWS-002…NEWS-010.

Deduplication collapses the same story from several sources into one row whose
``sources`` array lists every outlet that carried it. Credibility tier and
verification status are stored, not recomputed at read time, so a decision can be
replayed against exactly the verification state that applied when it was made.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import VerificationStatus
from app.core.ids import new_id
from app.db.base import JSONColumn, RATIO, Base, TimestampMixin

__all__ = ["NewsSource", "NewsItem"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class NewsSource(TimestampMixin, Base):
    """A configured source with its credibility tier (NEWS-004)."""

    __tablename__ = "news_sources"
    __table_args__ = (sa.UniqueConstraint("slug", name="news_sources_slug"),)

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("nsr"))
    slug: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    publisher_id: Mapped[Optional[str]] = mapped_column(sa.String(128), nullable=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    #: 1 = exchange filings/regulator, 2 = established media, 3 = aggregator, 4 = social.
    tier: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="rss")
    endpoint: Mapped[Optional[str]] = mapped_column(sa.String(512), nullable=True)
    weight: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=1)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)


class NewsItem(TimestampMixin, Base):
    __tablename__ = "news_items"
    __table_args__ = (
        sa.UniqueConstraint("dedupe_key", name="news_items_dedupe_key"),
        sa.Index("ix_news_published", "published_at"),
        sa.Index("ix_news_verification", "verification_status"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("nws"))

    #: Canonical URL hash plus normalised-title fingerprint (NEWS-002).
    dedupe_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    data_origin: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(sa.String(1024), nullable=True)
    url_hash: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    #: Stored verbatim — the grounding check anchors LLM claims against this.
    body: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    published_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    #: Every source that carried this story: ``[{slug, tier, url, fetched_at}]``.
    sources: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    #: Best (lowest-numbered) tier among the sources.
    best_tier: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3)
    source_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    #: ``[{instrument_id, symbol, confidence}]`` (NEWS-003).
    entities: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)

    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
    )
    verification_detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    conflicts_with: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)

    #: Structured LLM interpretation (NEWS-007); never free text used for execution.
    interpretation: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)
    sentiment_score: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    event_type: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)
    llm_call_id: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    #: Claims dropped for lacking an anchor in the source text (NEWS-009).
    unsourced_claims: Mapped[Optional[list[Any]]] = mapped_column(JSONColumn, nullable=True)

    @property
    def is_actionable(self) -> bool:
        """Verified news only. Everything else is context, not justification."""
        return self.verification_status is VerificationStatus.VERIFIED
