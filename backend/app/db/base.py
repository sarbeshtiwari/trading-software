"""Declarative base, shared column types and mixins.

Conventions
-----------
* **Explicit constraint names.** The naming convention makes Alembic
  autogeneration deterministic; without it, dropping a constraint in a migration
  depends on whatever name the database invented.
* **Decimal columns, never float.** ``MONEY`` is 2 dp (paise), ``PRICE`` is 4 dp
  (tick sizes go to 0.05 but option premia and index values need headroom),
  ``QTY_DECIMAL`` covers computed quantities. See ARCH-014.
* **Timezone-aware timestamps.** Every timestamp column is ``TIMESTAMPTZ``.
* **JSON columns** use ``JSONB`` on PostgreSQL and plain ``JSON`` elsewhere, so
  the model layer can still be exercised on SQLite in fast unit tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.clock import get_clock

__all__ = [
    "Base",
    "metadata",
    "MONEY",
    "PRICE",
    "QTY_DECIMAL",
    "RATIO",
    "JSONColumn",
    "TimestampMixin",
    "utcnow_default",
]

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

#: Currency amounts, quantised to paise.
MONEY = sa.Numeric(20, 2)
#: Prices; 4 dp accommodates option premia and index levels.
PRICE = sa.Numeric(18, 4)
#: Computed quantities that may be fractional before lot rounding.
QTY_DECIMAL = sa.Numeric(18, 4)
#: Ratios, percentages and Greeks.
RATIO = sa.Numeric(12, 6)

#: JSONB on PostgreSQL, JSON elsewhere.
JSONColumn = sa.JSON().with_variant(JSONB, "postgresql")


def utcnow_default() -> datetime:
    """Default timestamp, taken from the injected clock (ARCH-012)."""
    return get_clock().utcnow()


class Base(DeclarativeBase):
    metadata = metadata

    type_annotation_map = {
        dict[str, Any]: JSONColumn,
        list[Any]: JSONColumn,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk!r}>"


class TimestampMixin:
    """``created_at``/``updated_at`` maintained by the application clock."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=utcnow_default,
        index=True,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
        default=utcnow_default,
        onupdate=utcnow_default,
    )
