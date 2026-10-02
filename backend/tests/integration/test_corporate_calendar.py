"""Synthetic corporate-calendar revisions and event-window integration."""

from datetime import timedelta

import pytest

from app.analysis.events import event_blackout
from app.analysis.fundamental.calendar import (
    CorporateCalendarEvidence,
    CorporateCalendarStore,
    CorporateEventEvidence,
)
from app.core.clock import FakeClock
from tests.integration.test_fundamentals import seed
from tests.unit.test_option_chain import OBSERVED


def calendar_evidence(**changes):
    fields = {
        "instrument_id": "ins-test",
        "symbol": "TEST",
        "source": "manual-test",
        "known_at": OBSERVED,
        "coverage_start": OBSERVED - timedelta(days=10),
        "coverage_end": OBSERVED + timedelta(days=30),
        "events": (
            CorporateEventEvidence(id="results", kind="RESULTS", at=OBSERVED + timedelta(days=2)),
        ),
    }
    fields.update(changes)
    return CorporateCalendarEvidence(**fields)


async def test_corporate_calendar(db_engine):
    await seed()
    clock = FakeClock(OBSERVED)
    store = CorporateCalendarStore(clock)
    original = calendar_evidence()
    identifier = await store.import_json(original.model_dump_json())
    assert await store.import_json(original.model_dump_json()) == identifier
    stored = await store.at("ins-test", source="manual-test", as_of=OBSERVED)
    assert (
        stored.upcoming(start=OBSERVED, end=OBSERVED + timedelta(days=3), as_of=OBSERVED)
        == original.events
    )
    calendar = stored.blackouts(before=timedelta(days=2), after=timedelta(days=1))
    assert (
        event_blackout(
            "TEST", as_of=OBSERVED, calendar=calendar, restricted_strategies=("intraday",)
        ).status
        == "BLACKOUT"
    )
    clock.advance(timedelta(hours=1))
    await store.import_json(calendar_evidence(known_at=clock.now(), events=()).model_dump_json())
    assert (
        await store.at("ins-test", source="manual-test", as_of=OBSERVED)
    ).events == original.events
    assert (await store.at("ins-test", source="manual-test", as_of=clock.now())).events == ()


async def test_calendar_late_receipt_and_conflict(db_engine):
    await seed()
    store = CorporateCalendarStore(FakeClock(OBSERVED + timedelta(minutes=1)))
    await store.import_json(calendar_evidence().model_dump_json())
    assert await store.at("ins-test", source="manual-test", as_of=OBSERVED) is None
    with pytest.raises(ValueError, match="conflicting"):
        await store.import_json(calendar_evidence(events=()).model_dump_json())


def test_calendar_blackout_coverage_is_conservative():
    calendar = calendar_evidence()
    windows = calendar.blackouts(before=timedelta(days=2), after=timedelta(days=1))
    assert windows.active("TEST", calendar.coverage_end) is None
    assert windows.active("TEST", calendar.coverage_start) is None
    with pytest.raises(ValueError, match="coverage"):
        calendar.upcoming(start=OBSERVED, end=OBSERVED + timedelta(days=31), as_of=OBSERVED)
