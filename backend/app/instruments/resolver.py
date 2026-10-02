"""Instrument lookup — supports GRW-016, FUT-001, OPT-001.

An in-memory index over the instrument master. Every hot path needs lot size and
tick size, and hitting the database for them on every order would put a query in
the middle of the execution path.

Lookups return ``None`` rather than raising when an instrument is unknown: the
caller decides whether that is fatal (an order) or merely a skip (a watchlist
entry). ``require`` is there for the cases where it is fatal.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

import sqlalchemy as sa

from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.instrument import Instrument

logger = get_logger("instruments.resolver")

__all__ = ["InstrumentResolver", "get_resolver"]


def _key(exchange: Exchange, segment: Segment, symbol: str) -> str:
    return f"{exchange.value}_{segment.value}_{symbol}"


class InstrumentResolver:
    def __init__(self) -> None:
        self._by_key: dict[str, Instrument] = {}
        self._by_isin: dict[str, list[Instrument]] = {}
        self._by_token: dict[str, Instrument] = {}
        self._by_underlying: dict[str, list[Instrument]] = {}
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def size(self) -> int:
        return len(self._by_key)

    async def refresh(self) -> int:
        """Rebuild the index from the active instrument master."""
        async with db_session.session_scope() as session:
            rows = (
                (await session.execute(
                    sa.select(Instrument).where(Instrument.is_active.is_(True))
                ))
                .scalars()
                .all()
            )
            # Detach from the session so the cache does not hold live ORM state.
            for row in rows:
                session.expunge(row)

        self._by_key = {}
        self._by_isin = {}
        self._by_token = {}
        self._by_underlying = {}

        for row in rows:
            self._by_key[_key(row.exchange, row.segment, row.trading_symbol)] = row
            if row.isin:
                self._by_isin.setdefault(row.isin, []).append(row)
            if row.exchange_token:
                self._by_token[row.exchange_token] = row
            if row.underlying:
                self._by_underlying.setdefault(row.underlying.upper(), []).append(row)

        self._loaded = True
        logger.info("Instrument index refreshed", extra={"instruments": len(self._by_key)})
        return len(self._by_key)

    # --- Lookups ----------------------------------------------------------

    def get(
        self,
        trading_symbol: str,
        exchange: Exchange = Exchange.NSE,
        segment: Segment = Segment.CASH,
    ) -> Optional[Instrument]:
        return self._by_key.get(_key(exchange, segment, trading_symbol))

    def require(
        self,
        trading_symbol: str,
        exchange: Exchange = Exchange.NSE,
        segment: Segment = Segment.CASH,
    ) -> Instrument:
        instrument = self.get(trading_symbol, exchange, segment)
        if instrument is None:
            raise NotFoundError(
                f"Unknown instrument {exchange.value}/{segment.value}/{trading_symbol}. "
                f"Has the instrument master been loaded?",
                context={"trading_symbol": trading_symbol, "index_size": self.size},
            )
        return instrument

    def by_isin(self, isin: str) -> list[Instrument]:
        return list(self._by_isin.get(isin, ()))

    def by_token(self, token: str) -> Optional[Instrument]:
        return self._by_token.get(token)

    # --- Derivatives ------------------------------------------------------

    def derivatives_for(self, underlying: str) -> list[Instrument]:
        return list(self._by_underlying.get(underlying.upper(), ()))

    def expiries_for(
        self, underlying: str, instrument_type: Optional[InstrumentType] = None
    ) -> list[date]:
        """Available expiries, ascending."""
        expiries = {
            row.expiry_date
            for row in self.derivatives_for(underlying)
            if row.expiry_date is not None
            and (instrument_type is None or row.instrument_type is instrument_type)
        }
        return sorted(expiries)

    def nearest_expiry(
        self,
        underlying: str,
        on: date,
        *,
        instrument_type: Optional[InstrumentType] = None,
        min_days: int = 0,
    ) -> Optional[date]:
        """Nearest expiry at least ``min_days`` away (OPT-003)."""
        for expiry in self.expiries_for(underlying, instrument_type):
            if (expiry - on).days >= min_days:
                return expiry
        return None

    def future_for(
        self, underlying: str, expiry: Optional[date] = None, *, on: Optional[date] = None
    ) -> Optional[Instrument]:
        """The futures contract for an underlying, defaulting to the near month."""
        candidates = [
            row
            for row in self.derivatives_for(underlying)
            if row.instrument_type is InstrumentType.FUTURE
        ]
        if expiry is not None:
            for row in candidates:
                if row.expiry_date == expiry:
                    return row
            return None
        dated = sorted(
            (row for row in candidates if row.expiry_date is not None),
            key=lambda row: row.expiry_date,  # type: ignore[arg-type]
        )
        if on is not None:
            dated = [row for row in dated if row.expiry_date >= on]  # type: ignore[operator]
        return dated[0] if dated else None

    def option_for(
        self,
        underlying: str,
        expiry: date,
        strike: Decimal,
        option_type: OptionType,
    ) -> Optional[Instrument]:
        for row in self.derivatives_for(underlying):
            if (
                row.instrument_type is InstrumentType.OPTION
                and row.expiry_date == expiry
                and row.option_type is option_type
                and row.strike_price is not None
                and Decimal(row.strike_price) == Decimal(strike)
            ):
                return row
        return None

    def strikes_for(self, underlying: str, expiry: date) -> list[Decimal]:
        strikes = {
            Decimal(row.strike_price)
            for row in self.derivatives_for(underlying)
            if row.instrument_type is InstrumentType.OPTION
            and row.expiry_date == expiry
            and row.strike_price is not None
        }
        return sorted(strikes)

    def search(self, fragment: str, limit: int = 20) -> Sequence[Instrument]:
        needle = fragment.strip().upper()
        if not needle:
            return ()
        matches = [
            row
            for row in self._by_key.values()
            if needle in row.trading_symbol.upper()
            or (row.name and needle in row.name.upper())
        ]
        return matches[:limit]


_resolver: Optional[InstrumentResolver] = None


def get_resolver() -> InstrumentResolver:
    global _resolver
    if _resolver is None:
        _resolver = InstrumentResolver()
    return _resolver
