"""External fixtures traverse ingestion, durable audit, stored source and replay."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from app.core.errors import SafetyError, ValidationError
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.marketdata.recorded import RecordedSnapshot
from app.marketdata.recordings import RecordingBundle, snapshot_record
from app.marketdata.stored_quotes import stored_quote
from app.trading.observations import ReferenceIngestion
from app.trading.worker import StoredQuoteSource
from tests.integration.test_fundamentals import seed
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_option_chain import OBSERVED


async def test_ingested_bands_survive_restart_and_recording_without_lookahead(
    db_engine, fake_clock
):
    await seed()
    fake_clock.set_to(OBSERVED)
    provider = fixture_provider()
    provider.data_origin = DataOrigin.LIVE
    original = replace(
        provider.get_quote.return_value,
        data_origin=DataOrigin.LIVE,
        lower_circuit=Decimal("90"),
        upper_circuit=Decimal("110"),
    )
    provider.get_quote.return_value = original
    await ReferenceIngestion(provider, clock=fake_clock).observe_quote("ins-test")
    assert await stored_quote(original.instrument, as_of=OBSERVED - timedelta(seconds=1)) is None
    recovered = await StoredQuoteSource(FakeClock(OBSERVED))(original.instrument)
    assert recovered == original
    record = snapshot_record(RecordedSnapshot(provider.name, OBSERVED, recovered))
    bundle = RecordingBundle(
        format_version=1, source="isolated test", candles=(), snapshots=(record,)
    )
    restored = RecordingBundle.model_validate_json(bundle.model_dump_json())
    clock = FakeClock(OBSERVED - timedelta(seconds=1))
    replay = restored.provider(clock=clock)
    with pytest.raises(ValidationError, match="not yet available"):
        await replay.get_quote(original.instrument)
    assert await replay.step() == OBSERVED
    observed = await replay.get_quote(original.instrument)
    assert observed.lower_circuit == Decimal("90")
    assert observed.upper_circuit == Decimal("110")
    assert observed.observed_at == OBSERVED
    async with db_session.session_scope() as session:
        event = await session.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.event_type == "REFERENCE_QUOTE_SNAPSHOT",
            )
        )
        event.data_used = {
            **event.data_used,
            "quote": {**event.data_used["quote"], "upper_circuit": "200"},
        }
    with pytest.raises(SafetyError, match="INTEGRITY"):
        await StoredQuoteSource(FakeClock(OBSERVED))(original.instrument)


@pytest.mark.parametrize("upper", [Decimal("NaN"), Decimal("Infinity"), Decimal("80")])
async def test_invalid_band_is_rejected_at_ingestion(db_engine, fake_clock, upper):
    await seed()
    fake_clock.set_to(OBSERVED)
    provider = fixture_provider()
    provider.get_quote.return_value = replace(
        provider.get_quote.return_value, lower_circuit=Decimal("90"), upper_circuit=upper
    )
    with pytest.raises(ValueError, match="invalid"):
        await ReferenceIngestion(provider, clock=fake_clock).observe_quote("ins-test")
