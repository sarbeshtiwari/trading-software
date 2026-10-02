"""Instrument master loader — GRW-016.

Groww publishes the tradable universe as a CSV. Everything downstream depends on
it being right: lot size decides position size (SIZE-002), tick size decides
whether a limit price is even accepted (EXCH-004), and expiry decides when an F&O
position must be closed (EXCH-006).

Two decisions worth stating:

* **Header names are resolved, not assumed.** The CSV column names are not part
  of the published API contract, so the loader accepts the known spellings and
  fails with the actual header list when a *required* column is missing — rather
  than silently loading an instrument master with no lot sizes.
* **Upsert, never truncate-and-reload.** Wiping the table would momentarily leave
  the system with no instruments, and anything running concurrently would see an
  empty universe.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.groww.endpoints import Endpoints
from app.config import Settings, get_settings
from app.core.clock import Clock, get_clock
from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.core.errors import InvalidResponseError
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.instrument import Instrument

logger = get_logger("instruments.loader")

__all__ = ["InstrumentRow", "LoadResult", "InstrumentLoader", "parse_instrument_csv"]

#: Canonical field -> accepted CSV header spellings, lowercased.
_HEADERS: dict[str, tuple[str, ...]] = {
    "exchange": ("exchange",),
    "segment": ("segment",),
    "trading_symbol": ("trading_symbol", "tradingsymbol", "symbol"),
    "groww_symbol": ("groww_symbol", "growwsymbol"),
    "name": ("name", "company_name", "instrument_name"),
    "isin": ("isin",),
    "exchange_token": ("exchange_token", "token", "instrument_token"),
    "instrument_type": ("instrument_type", "instrumenttype", "series"),
    "lot_size": ("lot_size", "lotsize"),
    "tick_size": ("tick_size", "ticksize"),
    "freeze_quantity": ("freeze_quantity", "freeze_qty"),
    "underlying": ("underlying", "underlying_symbol", "name_underlying"),
    "expiry_date": ("expiry_date", "expiry"),
    "strike_price": ("strike_price", "strike"),
    "option_type": ("option_type", "instrument_subtype"),
    "is_weekly": ("is_weekly", "weekly"),
}

#: Without these the row cannot be trusted enough to trade on.
_REQUIRED = ("exchange", "segment", "trading_symbol")


@dataclass
class InstrumentRow:
    exchange: Exchange
    segment: Segment
    trading_symbol: str
    instrument_type: InstrumentType
    lot_size: int = 1
    tick_size: Decimal = Decimal("0.05")
    groww_symbol: Optional[str] = None
    exchange_token: Optional[str] = None
    isin: Optional[str] = None
    name: Optional[str] = None
    freeze_quantity: Optional[int] = None
    underlying: Optional[str] = None
    expiry_date: Optional[date] = None
    strike_price: Optional[Decimal] = None
    option_type: Optional[OptionType] = None
    is_weekly_expiry: Optional[bool] = None

    def as_values(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "segment": self.segment,
            "trading_symbol": self.trading_symbol,
            "instrument_type": self.instrument_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "groww_symbol": self.groww_symbol,
            "exchange_token": self.exchange_token,
            "isin": self.isin,
            "name": self.name,
            "freeze_quantity": self.freeze_quantity,
            "underlying": self.underlying,
            "expiry_date": self.expiry_date,
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "is_weekly_expiry": self.is_weekly_expiry,
            "is_fno_eligible": self.segment is Segment.FNO,
            "is_active": True,
            "source": "groww_csv",
        }


@dataclass
class LoadResult:
    parsed: int = 0
    inserted: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed,
            "inserted": self.inserted,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "skipped": self.skipped,
            "skip_reasons": self.skip_reasons,
        }


# --- Parsing --------------------------------------------------------------


def _resolve_headers(fieldnames: Sequence[str]) -> dict[str, str]:
    """Map canonical names to the actual CSV headers present."""
    lowered = {name.strip().lower(): name for name in fieldnames if name}
    resolved: dict[str, str] = {}
    for canonical, candidates in _HEADERS.items():
        for candidate in candidates:
            if candidate in lowered:
                resolved[canonical] = lowered[candidate]
                break

    missing = [name for name in _REQUIRED if name not in resolved]
    if missing:
        raise InvalidResponseError(
            f"Instrument CSV is missing required columns {missing}. "
            f"Headers present: {sorted(lowered)}",
            context={"missing": missing, "headers": sorted(lowered)},
        )
    return resolved


def _text(row: Mapping[str, Any], headers: Mapping[str, str], key: str) -> Optional[str]:
    header = headers.get(key)
    if header is None:
        return None
    value = row.get(header)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(row, headers, key) -> Optional[Decimal]:  # type: ignore[no-untyped-def]
    text = _text(row, headers, key)
    if text is None:
        return None
    try:
        value = Decimal(text)
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def _int(row, headers, key) -> Optional[int]:  # type: ignore[no-untyped-def]
    text = _text(row, headers, key)
    if text is None:
        return None
    try:
        value = Decimal(text)
        if not value.is_finite() or value != value.to_integral_value():
            return None
        return int(value)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(row, headers, key) -> Optional[date]:  # type: ignore[no-untyped-def]
    text = _text(row, headers, key)
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _classify(
    segment: Segment,
    raw_type: Optional[str],
    option_type: Optional[OptionType],
    expiry: Optional[date],
) -> InstrumentType:
    """Decide the instrument type from whatever the row actually carries."""
    text = (raw_type or "").upper()
    if option_type is not None or text in {"CE", "PE", "OPTIDX", "OPTSTK", "OPTION"}:
        return InstrumentType.OPTION
    if text in {"FUTIDX", "FUTSTK", "FUT", "FUTURE"}:
        return InstrumentType.FUTURE
    if text in {"INDEX", "IDX"}:
        return InstrumentType.INDEX
    if segment is Segment.FNO:
        # In the F&O segment an expiry with no option type is a future.
        return InstrumentType.FUTURE if expiry is not None else InstrumentType.INDEX
    return InstrumentType.EQUITY


def parse_instrument_csv(content: str, result: Optional[LoadResult] = None) -> list[InstrumentRow]:
    """Parse the instrument CSV into typed rows, skipping what cannot be trusted."""
    outcome = result or LoadResult()
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise InvalidResponseError("Instrument CSV had no header row")

    headers = _resolve_headers(reader.fieldnames)
    rows: list[InstrumentRow] = []

    for raw in reader:
        symbol = _text(raw, headers, "trading_symbol")
        exchange_text = _text(raw, headers, "exchange")
        segment_text = _text(raw, headers, "segment")
        if not symbol or not exchange_text or not segment_text:
            outcome.note_skip("missing_identity")
            continue

        try:
            exchange = Exchange(exchange_text.upper())
        except ValueError:
            outcome.note_skip(f"unknown_exchange:{exchange_text.upper()}")
            continue
        try:
            segment = Segment(segment_text.upper())
        except ValueError:
            outcome.note_skip(f"unknown_segment:{segment_text.upper()}")
            continue

        raw_type = _text(raw, headers, "instrument_type")
        option_text = _text(raw, headers, "option_type")
        if not option_text and raw_type and raw_type.upper() in {"CE", "PE"}:
            option_text = raw_type
        option_type: Optional[OptionType] = None
        if option_text and option_text.upper() in {"CE", "PE"}:
            option_type = OptionType(option_text.upper())

        expiry = _date(raw, headers, "expiry_date")
        instrument_type = _classify(segment, raw_type, option_type, expiry)

        strike_price = _decimal(raw, headers, "strike_price")
        if instrument_type in {InstrumentType.OPTION, InstrumentType.FUTURE} and expiry is None:
            outcome.note_skip("missing_derivative_expiry")
            continue
        if instrument_type is InstrumentType.OPTION and (
            option_type is None or strike_price is None or strike_price <= 0
        ):
            outcome.note_skip("invalid_option_contract")
            continue

        lot_size = _int(raw, headers, "lot_size")
        if lot_size is None or lot_size <= 0 or lot_size > 2147483647:
            outcome.note_skip("invalid_lot_size")
            continue

        tick_size = _decimal(raw, headers, "tick_size")
        if tick_size is None or tick_size <= 0:
            outcome.note_skip("invalid_tick_size")
            continue

        weekly_text = _text(raw, headers, "is_weekly")
        rows.append(
            InstrumentRow(
                exchange=exchange,
                segment=segment,
                trading_symbol=symbol,
                instrument_type=instrument_type,
                lot_size=lot_size,
                tick_size=tick_size,
                groww_symbol=_text(raw, headers, "groww_symbol"),
                exchange_token=_text(raw, headers, "exchange_token"),
                isin=_text(raw, headers, "isin"),
                name=_text(raw, headers, "name"),
                freeze_quantity=_int(raw, headers, "freeze_quantity"),
                underlying=_text(raw, headers, "underlying"),
                expiry_date=expiry,
                strike_price=strike_price if instrument_type is InstrumentType.OPTION else None,
                option_type=option_type,
                is_weekly_expiry=(
                    weekly_text.lower() in {"true", "1", "yes", "y"} if weekly_text else None
                ),
            )
        )

    outcome.parsed = len(rows)
    return rows


# --- Loading --------------------------------------------------------------


class InstrumentLoader:
    """Downloads, parses and upserts the instrument master."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport
        self._clock = clock or get_clock()

    async def download(self) -> str:
        url = Endpoints.INSTRUMENTS_CSV.resolve()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0), transport=self._transport
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            logger.info(
                "Downloaded instrument master",
                extra={"url": url, "bytes": len(response.content)},
            )
            return response.text

    async def load(
        self, content: Optional[str] = None, *, deactivate_missing: bool = True
    ) -> LoadResult:
        """Parse and upsert. Idempotent: re-running changes nothing."""
        result = LoadResult()
        text = content if content is not None else await self.download()
        rows = parse_instrument_csv(text, result)

        if not rows:
            raise InvalidResponseError(
                "Instrument master parsed to zero instruments; refusing to apply it",
                context=result.to_dict(),
            )

        async with db_session.session_scope() as session:
            await self._upsert(session, rows, result)
            if deactivate_missing:
                await self._deactivate_missing(session, rows, result)

        logger.info("Instrument master loaded", extra=result.to_dict())
        return result

    async def _upsert(
        self, session: AsyncSession, rows: Iterable[InstrumentRow], result: LoadResult
    ) -> None:
        existing_rows = (await session.execute(sa.select(Instrument))).scalars().all()
        existing = {(row.exchange, row.segment, row.trading_symbol): row for row in existing_rows}

        for row in rows:
            key = (row.exchange, row.segment, row.trading_symbol)
            values = row.as_values()
            current = existing.get(key)
            if current is None:
                session.add(Instrument(**values))
                result.inserted += 1
                continue

            changed = False
            for field_name, value in values.items():
                if getattr(current, field_name) != value:
                    setattr(current, field_name, value)
                    changed = True
            if changed:
                result.updated += 1

    async def _deactivate_missing(
        self, session: AsyncSession, rows: Sequence[InstrumentRow], result: LoadResult
    ) -> None:
        """Mark instruments absent from the new file inactive.

        Deactivated, never deleted: an expired contract still appears in the
        journal and in past positions, and deleting the row would break that
        history.
        """
        present = {(row.exchange, row.segment, row.trading_symbol) for row in rows}
        existing_rows = (
            (await session.execute(sa.select(Instrument).where(Instrument.is_active.is_(True))))
            .scalars()
            .all()
        )
        for current in existing_rows:
            key = (current.exchange, current.segment, current.trading_symbol)
            if key not in present:
                current.is_active = False
                result.deactivated += 1
