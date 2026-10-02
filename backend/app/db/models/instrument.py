"""Instrument master — DB-004.

One row per tradable contract. Equity, index, futures and options all live here;
the derivative-specific columns are null for cash instruments.

``lot_size`` and ``tick_size`` are load-bearing: sizing rounds down to whole lots
(SIZE-002) and prices round to the tick (EXCH-004). A wrong lot size is a wrong
position size, so they are non-nullable with sane defaults for cash.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.core.ids import new_id
from app.db.base import PRICE, Base, TimestampMixin

__all__ = ["Instrument"]


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, values_callable=lambda e: [item.value for item in e])


class Instrument(TimestampMixin, Base):
    __tablename__ = "instruments"
    __table_args__ = (
        sa.UniqueConstraint(
            "exchange", "segment", "trading_symbol", name="instruments_exchange_segment_symbol"
        ),
        sa.Index("ix_instruments_isin", "isin"),
        sa.Index("ix_instruments_underlying_expiry", "underlying", "expiry_date"),
        sa.CheckConstraint("lot_size > 0", name="lot_size_positive"),
        sa.CheckConstraint("tick_size > 0", name="tick_size_positive"),
    )

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True, default=lambda: new_id("ins"))

    exchange: Mapped[Exchange] = mapped_column(_enum(Exchange, "exchange"), nullable=False)
    segment: Mapped[Segment] = mapped_column(_enum(Segment, "segment"), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        _enum(InstrumentType, "instrument_type"), nullable=False
    )

    #: Symbol used on the order API.
    trading_symbol: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    #: Symbol used on Groww's market-data endpoints when it differs.
    groww_symbol: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    #: Exchange token, where the instrument dump provides one.
    exchange_token: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    isin: Mapped[Optional[str]] = mapped_column(sa.String(16), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(sa.String(256), nullable=True)

    lot_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    tick_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0.05"))
    freeze_quantity: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)

    # --- Derivative fields (null for cash) --------------------------------
    underlying: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    expiry_date: Mapped[Optional[date]] = mapped_column(sa.Date, nullable=True)
    strike_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    option_type: Mapped[Optional[OptionType]] = mapped_column(
        _enum(OptionType, "option_type"), nullable=True
    )
    is_weekly_expiry: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)

    # --- Classification and state ----------------------------------------
    sector: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(sa.String(128), nullable=True)
    is_fno_eligible: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    #: F&O ban period / regulatory restriction (EXCH-008).
    is_restricted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    restriction_reason: Mapped[Optional[str]] = mapped_column(sa.String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, index=True)

    #: Source dump this row came from, for provenance.
    source: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    @property
    def key(self) -> str:
        """Canonical ``EXCHANGE_SEGMENT_SYMBOL`` key used in caches and feeds."""
        return f"{self.exchange.value}_{self.segment.value}_{self.trading_symbol}"

    @property
    def is_option(self) -> bool:
        return self.instrument_type is InstrumentType.OPTION

    @property
    def is_future(self) -> bool:
        return self.instrument_type is InstrumentType.FUTURE
