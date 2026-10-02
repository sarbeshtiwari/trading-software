"""Audit reads traverse the actual worker lifecycle and withhold altered evidence."""

from dataclasses import replace
from decimal import Decimal

import httpx
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.trading import Trade
from app.main import create_app
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.integration.test_reference_worker import credentials, setup_worker
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


async def test_rejection_query_filters_and_missing_chain(db_engine, credentials, fake_clock):
    fake_clock.set_to(OBSERVED)
    context = await setup_context()
    outcome = await DecisionPipeline(validator()).process(payload(confidence="0.5"), context)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": credentials[1]}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        response = await client.get(f"/api/v1/audit/trails/{outcome.candidate_id}")
        assert response.status_code == 200, response.text
        chain = response.json()["chains"][0]
        record = chain["records"][0]
        assert record["event_type"] == "NEGATIVE_DECISION"
        assert record["snapshot"]["result"]["reason_code"] == "CONFIDENCE_BELOW_FLOOR"
        params = {
            "decision_id": outcome.candidate_id,
            "instrument_id": record["instrument_id"],
            "day": OBSERVED.date().isoformat(),
        }
        response = await client.get("/api/v1/audit/events", params=params)
        assert [row["id"] for row in response.json()["events"]] == [record["id"]]
        response = await client.get("/api/v1/audit/events", params={**params, "day": "2000-01-01"})
        assert response.json()["events"] == []
        async with db_session.session_scope() as session:
            await session.execute(sa.delete(AuditEvent).where(AuditEvent.id == record["id"]))
        response = await client.get(f"/api/v1/audit/trails/{outcome.candidate_id}")
        assert response.json()["chains"][0]["integrity"] == "MISSING"
        assert response.json()["gaps"]


async def test_trade_drilldown_and_corruption(db_engine, credentials, fake_clock, tmp_path):
    worker, provider, client = await setup_worker(credentials, fake_clock, tmp_path, costed=True)
    try:
        await worker.cycle()
        quote = provider.get_quote.return_value
        provider.get_quote.return_value = replace(
            quote,
            ltp=Decimal(108),
            bids=(replace(quote.bids[0], price=Decimal(108)),),
            asks=(replace(quote.asks[0], price=Decimal("108.05")),),
        )
        await worker.cycle()
        assert not worker.failed, worker.detail
        async with db_session.session_scope() as session:
            fill = await session.scalar(sa.select(Trade))
            fill_id = fill.id
        response = await client.get(f"/api/v1/audit/trails/{fill_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        records = [record for chain in body["chains"] for record in chain["records"]]
        decision = next(record for record in records if record["event_type"] == "DECISION")
        assert decision["snapshot"]["position_size"]
        assert decision["snapshot"]["risk_calculation"]
        assert any(record["event_type"] == "PAPER_FILL_RECORDED" for record in records)
        assert body["journals"][0]["integrity"] == "CONSISTENT"
        assert Decimal(body["journals"][0]["snapshot"]["net_pnl"]) == Decimal("856.56")
        search = await client.get("/api/v1/audit/events", params={"trade_id": fill_id, "limit": 1})
        assert search.status_code == 200
        assert search.json()["has_more"]
        async with db_session.session_scope() as session:
            event = await session.get(AuditEvent, decision["id"])
            event.result = {"tampered": "MUST_NOT_BE_DISPLAYED"}
        response = await client.get(f"/api/v1/audit/trails/{fill_id}")
        corrupt = next(
            chain for chain in response.json()["chains"] if chain["id"] == decision["chain_id"]
        )
        assert corrupt["integrity"] == "CORRUPT"
        assert corrupt["records"] == []
        assert "MUST_NOT_BE_DISPLAYED" not in response.text
        assert (await client.get("/api/v1/audit/trails/unknown")).status_code == 404
        assert (await client.get("/api/v1/audit/events?limit=101")).status_code == 422
        client.headers.pop("Authorization")
        assert (await client.get("/api/v1/audit/events")).status_code == 401
        assert (await client.get(f"/api/v1/audit/trails/{fill_id}")).status_code == 401
    finally:
        await worker.stop()
        await client.aclose()
