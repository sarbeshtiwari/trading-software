"""Market-data tables — DB-005, DB-006, OC-007.

``candles`` and ``ticks`` become TimescaleDB hypertables in the initial migration.
Both are keyed so that re-ingesting an overlapping window upserts rather than
duplicating (HD-002): a duplicate bar would silently double volume and corrupt
every indicator computed from it.

Open interest is nullable throughout. For F&O instruments a missing OI must read
as "unknown", never as zero — zero OI is a tradable fact and would be interpreted
as such by the build-up classifier.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.data_origin import DataOrigin
from app.core.ids import new_id
from app.db.base import JSONColumn, PRICE, RATIO, Base, TimestampMixin

__all__ = ["Candle", "Tick", "OptionChainSnapshot"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class Candle(Base):
    """OHLCV(+OI) bar. Primary key includes the interval so timeframes coexist."""

    __tablename__ = "candles"
    __table_args__ = (
        sa.PrimaryKeyConstraint("instrument_id", "interval_minutes", "ts", name="pk_candles"),
        sa.Index("ix_candles_instrument_ts", "instrument_id", "ts"),
        sa.Index("candles_ts_idx", sa.text("ts DESC")),
        sa.CheckConstraint("high >= low", name="high_ge_low"),
        sa.CheckConstraint("interval_minutes > 0", name="interval_positive"),
        sa.CheckConstraint("volume >= 0", name="volume_non_negative"),
    )

    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    #: 1, 5, 10, 60, 240, 1440 (daily), 10080 (weekly) — matching Groww's intervals.
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    open_interest: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)

    data_origin: Mapped[DataOrigin] = mapped_column(
        _enum(DataOrigin, "data_origin"), nullable=False, default=DataOrigin.HISTORICAL
    )
    #: True when built locally from ticks rather than fetched from the broker.
    is_aggregated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    ingested_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Tick(Base):
    """Sampled tick capture for post-trade analysis (DB-006)."""

    __tablename__ = "ticks"
    __table_args__ = (
        sa.PrimaryKeyConstraint("instrument_id", "ts", name="pk_ticks"),
        sa.Index("ix_ticks_ts", "ts"),
    )

    instrument_id: Mapped[str] = mapped_column(
        sa.String(40), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    ltp: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    bid_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    bid_quantity: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)
    ask_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    ask_quantity: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)

    data_origin: Mapped[DataOrigin] = mapped_column(
        _enum(DataOrigin, "data_origin"), nullable=False, default=DataOrigin.LIVE
    )


class OptionChainSnapshot(TimestampMixin, Base):
    """Point-in-time option chain, retained for analysis and F&O backtests (OC-007)."""

    __tablename__ = "option_chain_snapshots"
    __table_args__ = (
        sa.UniqueConstraint("underlying", "expiry_date", "ts", name="option_chain_unique_snapshot"),
        sa.Index("ix_option_chain_underlying_ts", "underlying", "ts"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("ocs"))
    underlying: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    expiry_date: Mapped[Optional[datetime]] = mapped_column(sa.Date, nullable=True)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    spot_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    atm_strike: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    pcr_oi: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    pcr_volume: Mapped[Optional[Decimal]] = mapped_column(RATIO, nullable=True)
    max_pain_strike: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)

    #: Full strike ladder as stored by the chain model (OC-001).
    strikes: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    data_origin: Mapped[DataOrigin] = mapped_column(
        _enum(DataOrigin, "data_origin"), nullable=False, default=DataOrigin.LIVE
    )
