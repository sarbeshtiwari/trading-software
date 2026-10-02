"""Candle store, historical provider, backfill, warm-up, archive, instruments.

Covers HD-001, HD-002, HD-003, HD-007, HD-010 and GRW-016.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

import pytest
import sqlalchemy as sa

from app.core.clock import IST
from app.core.data_origin import DataOrigin
from app.core.enums import Exchange, InstrumentType, OptionType, Segment
from app.core.errors import InsufficientHistoryError, InvalidResponseError
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.db.models.market_data import Candle
from app.instruments.loader import InstrumentLoader, parse_instrument_csv
from app.instruments.resolver import InstrumentResolver
from app.marketdata.archive import ArchiveReporter
from app.marketdata.backfill import Backfiller
from app.marketdata.historical import HistoricalMarketDataProvider
from app.marketdata.ingest import CandleStore
from app.marketdata.models import Bar, InstrumentRef
from app.marketdata.warmup import WarmupRequirement, ensure_warmup

pytestmark = pytest.mark.integration

NIFTY = InstrumentRef("NIFTY26SEPFUT", Exchange.NSE, Segment.FNO)
BASE = datetime(2026, 1, 5, 9, 15, tzinfo=IST)


def _bars(count: int, *, start: datetime = BASE, interval: int = 5) -> list[Bar]:
    return [
        Bar(
            ts=start + timedelta(minutes=interval * index),
            open=Decimal("24500") + index,
            high=Decimal("24520") + index,
            low=Decimal("24490") + index,
            close=Decimal("24510") + index,
            volume=1000 + index,
            open_interest=9_800_000 + index,
        )
        for index in range(count)
    ]


# --- HD-002 ---------------------------------------------------------------


async def test_candle_ingest_idempotent(db_engine) -> None:
    store = CandleStore()
    bars = _bars(5)

    first = await store.write("ins_nifty", 5, bars)
    assert first.written == 5
    assert first.rejected == 0

    # Re-ingesting an overlapping window must upsert, never duplicate: a doubled
    # bar would double the volume every indicator reads.
    second = await store.write(
        "ins_nifty", 5, bars[2:] + _bars(2, start=BASE + timedelta(minutes=25))
    )
    assert second.written == 5

    async with db_session.session_scope() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Candle)
            .where(Candle.instrument_id == "ins_nifty")
        )
    assert count == 7


async def test_ingest_rejects_inconsistent_bars(db_engine) -> None:
    store = CandleStore()
    bad = Bar(
        ts=BASE,
        open=Decimal("100"),
        high=Decimal("90"),
        low=Decimal("110"),
        close=Decimal("100"),
        volume=10,
    )
    result = await store.write("ins_bad", 5, [bad])
    assert result.written == 0
    assert result.rejected == 1


async def test_store_read_and_coverage(db_engine) -> None:
    store = CandleStore()
    await store.write("ins_nifty", 5, _bars(10))

    window = await store.read("ins_nifty", 5, BASE, BASE + timedelta(minutes=20))
    assert len(window) == 5
    assert window[0].ts == BASE
    assert window[0].open_interest == 9_800_000

    last_three = await store.last_n("ins_nifty", 5, 3)
    assert len(last_three) == 3
    assert last_three[-1].ts == BASE + timedelta(minutes=45)

    earliest, latest, count = await store.coverage("ins_nifty", 5)
    assert count == 10
    assert earliest == BASE
    assert latest == BASE + timedelta(minutes=45)


async def test_upsert_updates_values(db_engine) -> None:
    store = CandleStore()
    await store.write("ins_x", 5, [_bars(1)[0]])

    corrected = Bar(
        ts=BASE,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("0.5"),
        close=Decimal("1.5"),
        volume=99,
    )
    await store.write("ins_x", 5, [corrected])

    stored = await store.read("ins_x", 5, BASE, BASE)
    assert len(stored) == 1
    assert stored[0].volume == 99
    assert stored[0].close == Decimal("1.5")


# --- HD-003 ---------------------------------------------------------------


async def test_gap_backfill(db_engine) -> None:
    """Only the missing range is fetched, not the whole window again."""
    from app.core.calendar import TradingCalendar

    store = CandleStore()
    calendar = TradingCalendar(complete_years=[2026])

    all_bars = _bars(12, interval=60, start=BASE)
    # Store everything except a three-bar hole in the middle.
    await store.write("ins_gap", 60, all_bars[:2] + all_bars[5:7])

    fetch_calls: list[tuple[datetime, datetime]] = []

    async def fetch(
        instrument: InstrumentRef, interval: int, start: datetime, end: datetime
    ) -> Sequence[Bar]:
        fetch_calls.append((start, end))
        return [bar for bar in all_bars if start <= bar.ts <= end]

    backfiller = Backfiller(fetch, store=store, calendar=calendar)
    before = await backfiller.report("ins_gap", NIFTY, 60, BASE, BASE + timedelta(minutes=60 * 6))
    assert before.gap_count == 3

    after = await backfiller.backfill("ins_gap", NIFTY, 60, BASE, BASE + timedelta(minutes=60 * 6))
    assert after.gap_count == 0
    # One targeted call for the one contiguous hole.
    assert len(fetch_calls) == 1


async def test_backfill_does_nothing_when_complete(db_engine) -> None:
    from app.core.calendar import TradingCalendar

    store = CandleStore()
    await store.write("ins_full", 60, _bars(7, interval=60, start=BASE))

    calls: list[int] = []

    async def fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return []

    backfiller = Backfiller(fetch, store=store, calendar=TradingCalendar(complete_years=[2026]))
    report = await backfiller.backfill(
        "ins_full", NIFTY, 60, BASE, BASE + timedelta(minutes=60 * 6)
    )
    assert report.gap_count == 0
    assert not calls


# --- HD-001 ---------------------------------------------------------------


async def test_historical_provider_reads_the_store(db_engine) -> None:
    store = CandleStore()
    await store.write(NIFTY.key, 5, _bars(6))

    provider = HistoricalMarketDataProvider(store=store, resolver=InstrumentResolver())
    bars = await provider.get_candles(NIFTY, 5, BASE, BASE + timedelta(minutes=25))

    assert len(bars) == 6
    assert provider.data_origin is DataOrigin.HISTORICAL


async def test_historical_provider_fetches_and_stores_when_empty(db_engine) -> None:
    fetched = _bars(4)

    async def fetch(instrument, interval, start, end):  # type: ignore[no-untyped-def]
        return fetched

    provider = HistoricalMarketDataProvider(
        store=CandleStore(), fetch=fetch, resolver=InstrumentResolver(), auto_backfill=False
    )
    bars = await provider.get_candles(NIFTY, 5, BASE, BASE + timedelta(minutes=20))
    assert len(bars) == 4

    # Stored on the way through, so the next read needs no API call.
    stored = await CandleStore().read(NIFTY.key, 5, BASE, BASE + timedelta(minutes=20))
    assert len(stored) == 4


@pytest.mark.safety
async def test_historical_provider_refuses_to_serve_live_prices(db_engine) -> None:
    """A historical provider answering 'what is the price now' is the bug
    ARCH-008 exists to prevent."""
    from app.core.errors import ValidationError

    provider = HistoricalMarketDataProvider(store=CandleStore(), resolver=InstrumentResolver())
    with pytest.raises(ValidationError):
        await provider.get_quote(NIFTY)
    with pytest.raises(ValidationError):
        await provider.get_ltp([NIFTY])


# --- HD-007 ---------------------------------------------------------------


async def test_warmup_blocks_insufficient_history(db_engine) -> None:
    store = CandleStore()
    await store.write(NIFTY.key, 5, _bars(150))

    requirement = WarmupRequirement(interval_minutes=5, bars=200, reason="EMA-200")
    with pytest.raises(InsufficientHistoryError) as excinfo:
        await ensure_warmup(NIFTY, requirement, store=store)

    assert "needs 200" in str(excinfo.value)
    assert excinfo.value.context["shortfall"] == 50

    # With enough history it passes.
    await store.write(NIFTY.key, 5, _bars(250))
    report = await ensure_warmup(NIFTY, requirement, store=store)
    assert report.satisfied


# --- HD-010 ---------------------------------------------------------------


async def test_archive_coverage_report(db_engine) -> None:
    store = CandleStore()
    # Three days of 5-minute bars: well short of the 60-day minimum.
    await store.write(NIFTY.key, 5, _bars(300))

    reporter = ArchiveReporter(store=store, minimum_days=60)
    report = await reporter.report([(NIFTY.key, NIFTY)], intervals=(5,))

    assert len(report.series) == 1
    series = report.series[0]
    assert series.bars == 300
    assert not series.exceeds_broker_window

    warning = report.warning()
    assert warning is not None
    assert "90 days of intraday data" in warning
    assert "not statistically meaningful" in warning


# --- GRW-016 ---------------------------------------------------------------

CSV = """exchange,segment,trading_symbol,name,isin,instrument_type,lot_size,tick_size,expiry_date,strike_price,option_type,underlying,exchange_token
NSE,CASH,RELIANCE,Reliance Industries,INE002A01018,EQ,1,0.05,,,,,2885
NSE,CASH,TCS,Tata Consultancy,INE467B01029,EQ,1,0.05,,,,,11536
NSE,FNO,NIFTY26SEPFUT,Nifty Futures,,FUTIDX,75,0.05,2026-09-24,,,NIFTY,53001
NSE,FNO,NIFTY26SEP24500CE,Nifty 24500 CE,,OPTIDX,75,0.05,2026-09-24,24500,CE,NIFTY,53002
NSE,FNO,NIFTY26SEP24500PE,Nifty 24500 PE,,OPTIDX,75,0.05,2026-09-24,24500,PE,NIFTY,53003
MCX,CASH,GOLD,Gold,,FUTCOM,100,1,,,,,99001
NSE,CASH,BADLOT,Bad Lot,,EQ,0,0.05,,,,,99002
"""


def test_instrument_csv_parsing() -> None:
    from app.instruments.loader import LoadResult

    result = LoadResult()
    rows = parse_instrument_csv(CSV, result)

    # MCX is a valid exchange but the bad lot size row is dropped.
    symbols = {row.trading_symbol for row in rows}
    assert "RELIANCE" in symbols
    assert "BADLOT" not in symbols
    assert result.skip_reasons.get("invalid_lot_size") == 1

    future = next(row for row in rows if row.trading_symbol == "NIFTY26SEPFUT")
    assert future.instrument_type is InstrumentType.FUTURE
    assert future.lot_size == 75
    assert future.underlying == "NIFTY"
    assert future.expiry_date.isoformat() == "2026-09-24"

    call = next(row for row in rows if row.trading_symbol == "NIFTY26SEP24500CE")
    assert call.instrument_type is InstrumentType.OPTION
    assert call.option_type is OptionType.CE
    assert call.strike_price == Decimal("24500")


def test_instrument_csv_missing_required_columns_is_loud() -> None:
    with pytest.raises(InvalidResponseError) as excinfo:
        parse_instrument_csv("name,isin\nfoo,bar\n")
    assert "missing required columns" in str(excinfo.value)


async def test_instrument_loader(db_engine) -> None:
    loader = InstrumentLoader()
    result = await loader.load(CSV)

    assert result.inserted == 6
    assert result.updated == 0
    assert result.skipped == 1

    async with db_session.session_scope() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Instrument))
    assert count == 6

    # Re-running is idempotent: nothing inserted, nothing changed.
    again = await loader.load(CSV)
    assert again.inserted == 0
    assert again.updated == 0


async def test_instrument_source_and_audit_commit_together(db_engine, monkeypatch):
    import hashlib

    from app.audit.service import AuditService
    from app.db.models.instrument_snapshot import InstrumentMasterSnapshot

    result = await InstrumentLoader().load(CSV)
    async with db_session.session_scope() as session:
        snapshot = await session.get(InstrumentMasterSnapshot, result.snapshot_id)
        assert snapshot.decoded_csv == CSV
        assert snapshot.content_sha256 == hashlib.sha256(CSV.encode("utf-8")).hexdigest()
        assert snapshot.source == "SUPPLIED_CSV"
        assert snapshot.outcome["inserted"] == 6
    assert await AuditService().verify(result.snapshot_id)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("isolated audit failure")

    monkeypatch.setattr(AuditService, "append_in_session", fail_audit)
    changed = CSV.replace("RELIANCE", "DIFFERENT")
    with pytest.raises(RuntimeError, match="isolated audit failure"):
        await InstrumentLoader().load(changed)
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(InstrumentMasterSnapshot))
            == 1
        )
        assert (
            await session.scalar(
                sa.select(Instrument).where(Instrument.trading_symbol == "DIFFERENT")
            )
            is None
        )
        original = await session.scalar(
            sa.select(Instrument).where(Instrument.trading_symbol == "RELIANCE")
        )
        assert original.is_active


async def test_instrument_loader_deactivates_missing_rows(db_engine) -> None:
    loader = InstrumentLoader()
    await loader.load(CSV)

    reduced = "\n".join(CSV.splitlines()[:3]) + "\n"
    result = await loader.load(reduced)

    assert result.deactivated == 4
    async with db_session.session_scope() as session:
        active = await session.scalar(
            sa.select(sa.func.count()).select_from(Instrument).where(Instrument.is_active.is_(True))
        )
        total = await session.scalar(sa.select(sa.func.count()).select_from(Instrument))
    # Deactivated, never deleted: past trades still reference these rows.
    assert active == 2
    assert total == 6


async def test_empty_instrument_master_is_refused(db_engine) -> None:
    loader = InstrumentLoader()
    header_only = CSV.splitlines()[0] + "\n"
    with pytest.raises(InvalidResponseError) as excinfo:
        await loader.load(header_only)
    assert "zero instruments" in str(excinfo.value)


async def test_duplicate_instrument_master_leaves_database_unchanged(db_engine):
    loader = InstrumentLoader()
    await loader.load(CSV)
    duplicate = CSV + CSV.splitlines()[1] + "\n"
    with pytest.raises(InvalidResponseError, match="Duplicate instrument"):
        await loader.load(duplicate)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Instrument)) == 6


async def test_duplicate_quarantine_excludes_every_conflicting_row(db_engine):
    loader = InstrumentLoader()
    await loader.load(CSV)
    duplicate = CSV + CSV.splitlines()[1].replace("0.05", "0.01") + "\n"
    result = await loader.load(duplicate, quarantine_duplicates=True)
    assert result.skip_reasons["quarantined_duplicate_identity"] == 2
    assert result.deactivated == 1
    async with db_session.session_scope() as session:
        original = await session.scalar(
            sa.select(Instrument).where(Instrument.trading_symbol == "RELIANCE")
        )
        assert not original.is_active
        assert original.tick_size == Decimal("0.05")
    with pytest.raises(InvalidResponseError, match="Duplicate"):
        await loader.load(duplicate, quarantine_duplicates=True, deactivate_missing=False)


async def test_instrument_refresh_preserves_manual_restriction(db_engine):
    loader = InstrumentLoader()
    await loader.load(CSV)
    async with db_session.session_scope() as session:
        instrument = await session.scalar(
            sa.select(Instrument).where(Instrument.trading_symbol == "TCS")
        )
        instrument.is_restricted = True
        instrument.restriction_reason = "OWNER_REVIEW_REQUIRED"
    await loader.load(CSV)
    async with db_session.session_scope() as session:
        instrument = await session.scalar(
            sa.select(Instrument).where(Instrument.trading_symbol == "TCS")
        )
        assert instrument.is_restricted
        assert instrument.restriction_reason == "OWNER_REVIEW_REQUIRED"


async def test_instrument_resolver(db_engine) -> None:
    await InstrumentLoader().load(CSV)

    resolver = InstrumentResolver()
    assert await resolver.refresh() == 6

    reliance = resolver.get("RELIANCE")
    assert reliance is not None
    assert reliance.lot_size == 1

    assert resolver.by_isin("INE467B01029")[0].trading_symbol == "TCS"
    assert resolver.by_token("53001").trading_symbol == "NIFTY26SEPFUT"

    from datetime import date

    expiries = resolver.expiries_for("NIFTY")
    assert expiries == [date(2026, 9, 24)]
    assert resolver.strikes_for("NIFTY", date(2026, 9, 24)) == [Decimal("24500")]

    call = resolver.option_for("NIFTY", date(2026, 9, 24), Decimal("24500"), OptionType.CE)
    assert call is not None
    assert call.trading_symbol == "NIFTY26SEP24500CE"

    future = resolver.future_for("NIFTY")
    assert future is not None
    assert future.trading_symbol == "NIFTY26SEPFUT"

    # An unknown instrument is a miss, and require() turns it into a hard error.
    from app.core.errors import NotFoundError

    assert resolver.get("NOSUCH") is None
    with pytest.raises(NotFoundError):
        resolver.require("NOSUCH")


async def test_resolver_min_days_to_expiry(db_engine) -> None:
    """OPT-003: a strategy needing two days to expiry must not get today."""
    from datetime import date

    await InstrumentLoader().load(CSV)
    resolver = InstrumentResolver()
    await resolver.refresh()

    assert resolver.nearest_expiry("NIFTY", date(2026, 9, 20), min_days=2) == date(2026, 9, 24)
    assert resolver.nearest_expiry("NIFTY", date(2026, 9, 24), min_days=2) is None
