"""Fundamental snapshots and the corporate calendar — DB-015, FUND-001, FUND-006.

Snapshots are **point-in-time**: a query asking "what did we know on 12 March"
returns the row whose ``as_of`` is on or before that date, never a later revision.
Without that, any backtest touching fundamentals leaks future information.

Note: Groww's API does not expose fundamentals. ``source`` records where each row
came from so a mixed-vendor history stays interpretable.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import JSONColumn, MONEY, PRICE, RATIO, Base, TimestampMixin

__all__ = ["FundamentalSnapshot", "CorporateEvent"]


class FundamentalSnapshot(TimestampMixin, Base):
    __tablename__ = "fundamentals"
    __table_args__ = (
        sa.UniqueConstraint("instrument_id", "as_of", "source", name="fundamentals_pit"),
        sa.Index("ix_fundamentals_instrument_asof", "instrument_id", "as_of"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("fnd"))
    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    #: The date this information was *known*, not the period it describes.
    as_of: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="manual")

    # --- Valuation (FUND-002) ---------------------------------------------
    market_cap: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    pe_ratio: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    pb_ratio: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    ev_ebitda: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    dividend_yield: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    book_value: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    # --- Growth and profitability (FUND-003) ------------------------------
    revenue: Mapped[Optional[Decimal]] = mapped_column(MONEY, nullable=True)
    revenue_growth_yoy: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    eps: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    eps_growth_yoy: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    roe: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    roce: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    operating_margin: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    net_margin: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)

    # --- Balance-sheet health (FUND-004) ----------------------------------
    debt_to_equity: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    interest_coverage: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    current_ratio: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    promoter_holding: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    promoter_pledge: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)

    # --- Composite (FUND-005) ---------------------------------------------
    composite_score: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    score_formula_version: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)
    score_components: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)

    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONColumn, nullable=True)


class CorporateEvent(TimestampMixin, Base):
    """Results, dividends, splits, bonuses, board meetings (FUND-006, HD-005)."""

    __tablename__ = "corporate_events"
    __table_args__ = (
        sa.Index("ix_corporate_events_instrument_date", "instrument_id", "event_date"),
        sa.UniqueConstraint(
            "instrument_id", "event_date", "event_type", name="corporate_events_unique"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("cev"))
    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    #: RESULTS | DIVIDEND | SPLIT | BONUS | BOARD_MEETING | AGM | BUYBACK | OTHER
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    event_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    ex_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    record_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)

    #: For splits/bonuses: the adjustment factor applied to pre-event prices.
    adjustment_factor: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    value: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="manual")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
