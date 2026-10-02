"""Actual provider ingestion and audited quote API, including stale/tampered evidence."""

from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.main import create_app
from app.trading.observations import ReferenceIngestion
from tests.integration.test_fundamentals import seed
from tests.integration.test_reference_ingestion import fixture_provider
from tests.unit.test_option_chain import OBSERVED

pytestmark = pytest.mark.usefixtures("authenticated_api")


async def test_quote_api_ingestion_origin_staleness_and_integrity(db_engine, fake_clock):
    await seed()
    fake_clock.set_to(OBSERVED)
    provider = fixture_provider()
    observation = await ReferenceIngestion(provider, clock=fake_clock).observe_quote("ins-test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        path = "/api/v1/market/quotes/ins-test"
        current = await client.get(path, params={"origin": "SYNTHETIC"})
        assert current.status_code == 200
        assert current.json()["status"] == "RECORDED"
        assert current.json()["ltp"] == "100"
        assert current.json()["change_pct"] is None
        assert current.json()["volume"] is None
        missing = await client.get(path, params={"origin": "LIVE"})
        assert missing.json()["status"] == "UNAVAILABLE"
        assert missing.json()["ltp"] is None
        fake_clock.advance(timedelta(minutes=1))
        stale = await client.get(path, params={"origin": "SYNTHETIC"})
        assert stale.json()["status"] == "STALE"
        assert stale.json()["ltp"] is None
        assert stale.json()["observed_at"] is not None
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.chain_id == observation.chain_id)
            )
            row.data_used = {**row.data_used, "provider": "tampered"}
        invalid = await client.get(path, params={"origin": "SYNTHETIC"})
        assert invalid.json()["status"] == "INVALID_EVIDENCE"
        assert invalid.json()["ltp"] is None
