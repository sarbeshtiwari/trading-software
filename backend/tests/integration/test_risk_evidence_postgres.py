"""Risk receipts on real PostgreSQL using rollback-only temporary tables."""

import os
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import session as db_session
from app.db.models.decision import RiskDecision
from app.risk.audit import RiskAudit
from app.risk.evidence import decision_integrity
from tests.unit.test_risk import limits, market, portfolio, proposal

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")


async def test_postgres_risk_receipt_survives_reload_and_detects_changed_amount(monkeypatch):
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in ("risk_decisions", "audit_events"):
                    await connection.execute(
                        sa.text(
                            f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"
                        )
                    )
                monkeypatch.setattr(
                    db_session,
                    "_sessionmaker",
                    async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    ),
                )
                identifier, _decision = await RiskAudit().evaluate_and_record(
                    proposal(risk_cost_per_unit=Decimal("0.0123456")),
                    portfolio(),
                    market(),
                    limits(),
                )
                async with db_session.session_scope() as session:
                    row = await session.get(RiskDecision, identifier)
                    assert await decision_integrity(session, row) == "AUDIT_BOUND"
                    assert row.evaluated_at.utcoffset().total_seconds() == 0
                    row.risk_amount = Decimal(1)
                async with db_session.session_scope() as session:
                    row = await session.get(RiskDecision, identifier)
                    assert await decision_integrity(session, row) == "CORRUPT"
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
