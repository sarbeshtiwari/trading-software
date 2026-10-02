"""Actual PAPER lifecycle and shared negative decisions through authenticated journal APIs."""

import csv
import io
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from app.agents.pipeline import DecisionPipeline
from app.audit.service import AuditService
from app.core.clock import UTC
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalAnnotation, JournalEntry
from app.main import create_app
from app.modes import TradingMode
from tests.integration.test_auth import credentials
from tests.integration.test_historical_api import login
from tests.integration.test_pipeline import setup_context
from tests.integration.test_proposal import validator
from tests.integration.test_reference_worker import setup_worker
from tests.quant_fixture import quant_decision
from tests.unit.test_option_chain import OBSERVED
from tests.unit.test_proposal import payload

__all__ = ["credentials"]


@pytest.fixture
async def journal_client(db_engine, credentials, fake_clock):
    fake_clock.set_to(OBSERVED)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        await login(client, credentials[1])
        yield client


async def negative(fake_clock):
    fake_clock.set_to(OBSERVED)
    context = await setup_context()
    result = await DecisionPipeline(validator()).process(payload(confidence="0.5"), context)
    return result, context


async def test_real_trade_lineage_and_accounting(db_engine, credentials, fake_clock, tmp_path):
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
        listed = await client.get("/api/v1/journal?kind=TRADE&outcome=WIN")
        assert listed.status_code == 200, listed.text
        row = listed.json()["entries"][0]
        response = await client.get(f"/api/v1/journal/{row['id']}")
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["lineage_status"] == "AVAILABLE", detail["gaps"]
        assert row["integrity"] == "AUDIT_BOUND"
        assert Decimal(row["gross_pnl"]) == 888
        assert Decimal(row["charges"]) == Decimal("31.44")
        assert Decimal(row["net_pnl"]) == Decimal("856.56")
        assert row["outcome"] == "WIN" and row["quantity"] == 111
        assert row["holding_period_seconds"] == 0 and not row["invalidation_fired"]
        assert Decimal(row["risk_amount"]) > 0 and Decimal(row["r_multiple"]) > 0
        assert row["ai_thesis"] is None and row["ai_confidence"] is None
        assert len(detail["orders"]) == len(detail["fills"]) == 2
        assert detail["risk_decisions"] and detail["sizing"] and detail["regime"]
        assert "approval_token" not in json.dumps(detail)
    finally:
        await worker.stop()
        await client.aclose()


async def test_rejections_annotations_exports_and_filters(journal_client, fake_clock):
    result, context = await negative(fake_clock)
    zero = await quant_decision(
        DecisionPipeline(validator()),
        payload(),
        context.model_copy(
            update={
                "portfolio": context.portfolio.model_copy(update={"available_margin": Decimal(0)})
            }
        ),
    )
    assert zero.code == "BUDGET_BELOW_MIN_LOT"
    listed = await journal_client.get("/api/v1/journal?kind=REJECTION&limit=1")
    assert listed.status_code == 200, listed.text
    assert listed.json()["has_more"]
    rows = (await journal_client.get("/api/v1/journal?kind=REJECTION")).json()["entries"]
    assert {row["rejection_code"] for row in rows} == {result.code, zero.code}
    assert all(row["net_pnl"] is None and row["quantity"] is None for row in rows)
    row = next(row for row in rows if row["rejection_code"] == result.code)
    identifier = row["id"]
    detail = (await journal_client.get(f"/api/v1/journal/{identifier}")).json()
    assert detail["candidate"]["id"] == result.candidate_id
    assert detail["orders"] == detail["fills"] == []
    assert row["rejection_rule"] == "VALIDATION"
    note = await journal_client.post(
        f"/api/v1/journal/{identifier}/annotations",
        json={
            "note": "=SUM(1,2) isolated annotation",
            "tags": ["reviewed", "no-trade"],
            "reason": "Owner reviewed the actual rejection",
        },
    )
    assert note.status_code == 201, note.text
    assert note.json()["author"] == "owner"
    response = await journal_client.get(f"/api/v1/journal/{identifier}")
    assert response.status_code == 200, response.text
    updated = response.json()["entry"]
    assert updated | {"annotations": []} == row
    assert updated["annotations"][0]["note"].startswith("=SUM")
    exported = await journal_client.get("/api/v1/journal/export/json")
    assert exported.status_code == 200, exported.text
    csv_response = await journal_client.get("/api/v1/journal/export/csv")
    assert csv_response.status_code == 200, csv_response.text
    parsed = [
        {key: json.loads(value) for key, value in record.items()}
        for record in csv.DictReader(io.StringIO(csv_response.text))
    ]
    assert parsed == exported.json()["entries"]
    assert set(JournalEntry.__table__.columns.keys()).issubset(parsed[0])
    assert (await journal_client.get("/api/v1/journal?instrument_id=absent")).json()[
        "entries"
    ] == []
    date = OBSERVED.date().isoformat()
    assert (
        len(
            (await journal_client.get(f"/api/v1/journal?start={date}&end={date}")).json()["entries"]
        )
        == 2
    )
    for query in (
        "limit=0",
        "offset=-1",
        "end=9999-12-31",
        "end=0001-01-01",
        "start=2026-02-02&end=2026-01-01",
    ):
        assert (await journal_client.get(f"/api/v1/journal?{query}")).status_code == 422
    assert (await journal_client.get("/api/v1/journal/export/json?offset=1")).status_code == 422
    assert (
        await journal_client.patch(f"/api/v1/journal/{identifier}", json={"net_pnl": 999})
    ).status_code == 405
    async with db_session.session_scope() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "JOURNAL_EXPORTED")
            )
            == 2
        )


@pytest.mark.parametrize("tamper", ["core", "note", "delete_note"])
async def test_tampered_records_fail_closed(db_engine, journal_client, fake_clock, tamper):
    await negative(fake_clock)
    identifier = (await journal_client.get("/api/v1/journal")).json()["entries"][0]["id"]
    response = await journal_client.post(
        f"/api/v1/journal/{identifier}/annotations",
        json={
            "note": "Original note",
            "reason": "Owner original observation",
        },
    )
    assert response.status_code == 201
    async with db_engine.begin() as connection:
        if tamper == "core":
            await connection.execute(
                sa.update(JournalEntry)
                .where(JournalEntry.id == identifier)
                .values(rejection_code="ALTERED")
            )
        elif tamper == "note":
            await connection.execute(sa.update(JournalAnnotation).values(note="ALTERED"))
        else:
            await connection.execute(sa.delete(JournalAnnotation))
    for suffix in ("", f"/{identifier}", "/export/json", "/export/csv"):
        assert (await journal_client.get(f"/api/v1/journal{suffix}")).status_code == 409


async def test_annotation_audit_failure_rolls_back(journal_client, fake_clock, monkeypatch):
    await negative(fake_clock)
    identifier = (await journal_client.get("/api/v1/journal")).json()["entries"][0]["id"]
    original = AuditService.append_in_session

    async def fail(self, session, identity, *args, **kwargs):
        if identity.event_type == "JOURNAL_ANNOTATION_ADDED":
            raise ValueError("injected audit failure")
        return await original(self, session, identity, *args, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    response = await journal_client.post(
        f"/api/v1/journal/{identifier}/annotations",
        json={
            "note": "Must never persist",
            "reason": "Simulated audit failure",
        },
    )
    assert response.status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalAnnotation)) == 0


async def test_journal_requires_authentication(db_engine):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        for path in ("", "/missing", "/export/json", "/export/csv"):
            assert (await client.get(f"/api/v1/journal{path}")).status_code == 401
        assert (
            await client.post("/api/v1/journal/missing/annotations", json={})
        ).status_code == 401
        assert (
            await client.post("/api/v1/journal/missing/corrections", json={})
        ).status_code == 401


async def test_ist_date_boundary_and_export_bound(journal_client):
    async with db_session.session_scope() as session:
        session.add_all(
            [
                JournalEntry(
                    id="before",
                    mode=TradingMode.PAPER,
                    created_at=datetime(2026, 1, 4, 18, 29, 59, tzinfo=UTC),
                ),
                JournalEntry(
                    id="after",
                    mode=TradingMode.PAPER,
                    created_at=datetime(2026, 1, 4, 18, 30, tzinfo=UTC),
                ),
            ]
        )
    response = await journal_client.get("/api/v1/journal?start=2026-01-05&end=2026-01-05")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["entries"]] == ["after"]
    detail = (await journal_client.get("/api/v1/journal/after")).json()
    assert detail["lineage_status"] == "INCOMPLETE"
    assert "LEGACY_JOURNAL_UNBOUND" in detail["gaps"]
    async with db_session.session_scope() as session:
        await session.execute(
            sa.insert(JournalEntry),
            [
                {"id": f"isolated-export-{index}", "mode": TradingMode.PAPER}
                for index in range(1999)
            ],
        )
    assert (await journal_client.get("/api/v1/journal/export/json")).status_code == 413


async def test_negative_journal_audit_failure_rolls_back(journal_client, fake_clock, monkeypatch):
    fake_clock.set_to(OBSERVED)
    context = await setup_context()
    original = AuditService.append_in_session

    async def fail(self, session, identity, *args, **kwargs):
        if identity.event_type == "REJECTION_JOURNAL_RECORDED":
            raise RuntimeError("injected rejection journal failure")
        return await original(self, session, identity, *args, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    with pytest.raises(RuntimeError, match="rejection journal failure"):
        await DecisionPipeline(validator()).process(payload(confidence="0.5"), context)
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 0
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "NEGATIVE_DECISION")
            )
            == 0
        )
