"""Candle store — HD-002.

Reads and writes the ``candles`` hypertable. Writes are **upserts** keyed on
(instrument, interval, timestamp): re-ingesting an overlapping window is a normal
consequence of gap backfill, and a duplicated bar would double the volume that
every volume-based indicator depends on.

Bars are validated before they are stored. An inconsistent bar rejected at write
time is one bug; the same bar stored and later feeding a strategy is a much
longer investigation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, ensure_ist, get_clock
from app.core.data_origin import DataOrigin
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.market_data import Candle
from app.marketdata.models import Bar
from app.marketdata.validation import validate_bar

logger = get_logger("marketdata.ingest")

__all__ = ["IngestResult", "CandleStore"]


@dataclass
class IngestResult:
    written: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note_rejection(self, reason: str) -> None:
        self.rejected += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, object]:
        return {"written": self.written, "rejected": self.rejected, "reasons": self.reasons}


class CandleStore:
    """Persistence for OHLCV bars."""

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self._clock = clock or get_clock()

    # --- Write ------------------------------------------------------------

    async def write(
        self,
        instrument_id: str,
        interval_minutes: int,
        bars: Sequence[Bar],
        *,
        data_origin: DataOrigin = DataOrigin.HISTORICAL,
        is_aggregated: bool = False,
        session: Optional[AsyncSession] = None,
    ) -> IngestResult:
        result = IngestResult()
        if not bars:
            return result

        rows: list[dict[str, object]] = []
        previous: Optional[Bar] = None
        for bar in bars:
            validation = validate_bar(bar, previous=previous)
            if not validation.ok:
                result.note_rejection(",".join(a.value for a in validation.fatal))
                continue
            previous = bar
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "interval_minutes": interval_minutes,
                    "ts": ensure_ist(bar.ts),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "open_interest": bar.open_interest,
                    "data_origin": data_origin,
                    "is_aggregated": is_aggregated,
                    "ingested_at": self._clock.utcnow(),
                }
            )

        if rows:
            if session is not None:
                await self._upsert(session, rows)
            else:
                async with db_session.session_scope() as owned:
                    await self._upsert(owned, rows)
            result.written = len(rows)

        if result.rejected:
            logger.warning(
                "Rejected bars during ingestion",
                extra={"instrument_id": instrument_id, **result.to_dict()},
            )
        return result

    async def _upsert(self, session: AsyncSession, rows: list[dict[str, object]]) -> None:
        dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(Candle).values(rows)
        update_columns = {
            column: getattr(statement.excluded, column)
            for column in (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_interest",
                "data_origin",
                "is_aggregated",
                "ingested_at",
            )
        }
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["instrument_id", "interval_minutes", "ts"],
                set_=update_columns,
            )
        )

    # --- Read -------------------------------------------------------------

    async def read(
        self,
        instrument_id: str,
        interval_minutes: int,
        start: datetime,
        end: datetime,
        *,
        limit: Optional[int] = None,
    ) -> list[Bar]:
        async with db_session.session_scope() as session:
            statement = (
                sa.select(Candle)
                .where(
                    Candle.instrument_id == instrument_id,
                    Candle.interval_minutes == interval_minutes,
                    Candle.ts >= ensure_ist(start),
                    Candle.ts <= ensure_ist(end),
                )
                .order_by(Candle.ts)
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = (await session.execute(statement)).scalars().all()

        return [
            Bar(
                ts=ensure_ist(row.ts),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                open_interest=row.open_interest,
            )
            for row in rows
        ]

    async def last_n(self, instrument_id: str, interval_minutes: int, count: int) -> list[Bar]:
        """The most recent ``count`` bars, oldest first."""
        async with db_session.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(Candle)
                        .where(
                            Candle.instrument_id == instrument_id,
                            Candle.interval_minutes == interval_minutes,
                        )
                        .order_by(Candle.ts.desc())
                        .limit(count)
                    )
                )
                .scalars()
                .all()
            )

        return [
            Bar(
                ts=ensure_ist(row.ts),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                open_interest=row.open_interest,
            )
            for row in reversed(rows)
        ]

    async def coverage(
        self, instrument_id: str, interval_minutes: int
    ) -> tuple[Optional[datetime], Optional[datetime], int]:
        """``(earliest, latest, count)`` for this series."""
        async with db_session.session_scope() as session:
            row = (
                await session.execute(
                    sa.select(
                        sa.func.min(Candle.ts),
                        sa.func.max(Candle.ts),
                        sa.func.count(),
                    ).where(
                        Candle.instrument_id == instrument_id,
                        Candle.interval_minutes == interval_minutes,
                    )
                )
            ).one()

        earliest, latest, count = row
        return (
            ensure_ist(earliest) if earliest else None,
            ensure_ist(latest) if latest else None,
            int(count or 0),
        )

    async def existing_timestamps(
        self, instrument_id: str, interval_minutes: int, start: datetime, end: datetime
    ) -> set[datetime]:
        async with db_session.session_scope() as session:
            rows = (
                await session.execute(
                    sa.select(Candle.ts).where(
                        Candle.instrument_id == instrument_id,
                        Candle.interval_minutes == interval_minutes,
                        Candle.ts >= ensure_ist(start),
                        Candle.ts <= ensure_ist(end),
                    )
                )
            ).scalars().all()
        return {ensure_ist(ts) for ts in rows}
