"""Canonical report-only payloads, independent of trading account state."""

import hashlib
from datetime import datetime

import sqlalchemy as sa

from app.audit.snapshots import canonical
from app.core.clock import UTC
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade

MAX_CATALOG_BYTES = 64 * 1024 * 1024


def column_values(row):
    return {
        key: value.replace(tzinfo=UTC)
        if isinstance(value, datetime) and value.tzinfo is None
        else value
        for key, value in row.items()
    }


async def load_catalog(session, run_id):
    payload = {}
    for key, model in (
        ("run", BacktestRun),
        ("results", BacktestResult),
        ("trades", BacktestTrade),
    ):
        table = model.__table__
        selector = table.c.id if key == "run" else table.c.run_id
        rows = (
            (await session.execute(sa.select(table).where(selector == run_id).order_by(table.c.id)))
            .mappings()
            .all()
        )
        payload[key] = [column_values(row) for row in rows]
    if len(payload["run"]) != 1:
        raise ValueError("one historical catalog run required")
    return payload


def catalog_digest(payload):
    encoded = canonical(payload).encode("utf-8")
    if len(encoded) > MAX_CATALOG_BYTES:
        raise ValueError("historical catalog exceeds publication size limit")
    return hashlib.sha256(encoded).hexdigest()
