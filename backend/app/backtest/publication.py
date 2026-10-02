"""Atomic publication of verified historical reports, never trading state."""

import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.backtest.catalog import catalog_digest, column_values, load_catalog
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.modes import TradingMode


def database_name(engine):
    database = engine.url.database or ""
    return Path(database).stem if engine.dialect.name == "sqlite" else database


async def publish_run(source_engine, run_id, recording_sha256, *, actor, clock):
    if (
        not re.fullmatch(r"[a-z0-9]{8,26}", run_id)
        or not re.fullmatch(r"[a-f0-9]{64}", recording_sha256)
        or database_name(source_engine) != f"ats_history_{run_id}"
        or database_name(db_session.get_engine()).startswith("ats_history_")
    ):
        raise ValueError("explicit historical source and non-historical catalog required")
    factory = async_sessionmaker(source_engine, expire_on_commit=False)
    async with factory() as source, source.begin():
        await source.execute(
            sa.select(BacktestRun.id).where(BacktestRun.id == run_id).with_for_update()
        )
        payload = await load_catalog(source, run_id)
        row = payload["run"][0]
        if (
            row["status"] not in {"COMPLETED", "INCOMPLETE", "FAILED"}
            or not row["simulated"]
            or row["assumptions"].get("recording_sha256") != recording_sha256
            or row["parameters"].get("recording_sha256") != recording_sha256
            or len(payload["results"]) != 1
        ):
            raise ValueError("historical report is not finalized for the declared recording")
        chain = list(
            (
                await source.scalars(
                    sa.select(AuditEvent)
                    .where(AuditEvent.chain_id == f"history:{run_id}")
                    .order_by(AuditEvent.sequence)
                )
            ).all()
        )
        digest = catalog_digest(payload)
        if (
            not chain
            or not verify_records(chain)
            or chain[-1].event_type != "HISTORICAL_RUN_FINISHED"
            or chain[-1].result.get("catalog_sha256") != digest
            or chain[-1].result.get("recording_sha256") != recording_sha256
            or chain[-1].result.get("status") != row["status"]
        ):
            raise ValueError("historical report audit binding invalid or unavailable")
        events = [
            column_values(
                {
                    column.name: getattr(event, column.name)
                    for column in AuditEvent.__table__.columns
                }
            )
            for event in chain
        ]
    async with db_session.session_scope() as target:
        existing = await target.get(BacktestRun, run_id)
        if existing is not None:
            publication = await target.scalar(
                sa.select(AuditEvent).where(
                    AuditEvent.chain_id == f"publication:{run_id}",
                    AuditEvent.event_type == "HISTORICAL_REPORT_PUBLISHED",
                )
            )
            imported_chain = list(
                (
                    await target.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == f"history:{run_id}")
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )
            publication_chain = list(
                (
                    await target.scalars(
                        sa.select(AuditEvent)
                        .where(AuditEvent.chain_id == f"publication:{run_id}")
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            )
            if (
                publication is None
                or publication.result.get("catalog_sha256") != digest
                or catalog_digest(await load_catalog(target, run_id)) != digest
                or not imported_chain
                or not verify_records(imported_chain)
                or not publication_chain
                or not verify_records(publication_chain)
            ):
                raise ValueError("historical catalog identity conflict; never overwrite")
            return "ALREADY_PUBLISHED"
        for key, model in (
            ("run", BacktestRun),
            ("results", BacktestResult),
            ("trades", BacktestTrade),
        ):
            if payload[key]:
                await target.execute(sa.insert(model.__table__), payload[key])
        await target.execute(sa.insert(AuditEvent.__table__), events)
        await AuditService(clock).append_in_session(
            target,
            AuditIdentity(
                chain_id=f"publication:{run_id}",
                event_type="HISTORICAL_REPORT_PUBLISHED",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {
                "result": {
                    "run_id": run_id,
                    "source_database": database_name(source_engine),
                    "catalog_sha256": digest,
                    "recording_sha256": recording_sha256,
                    "source_audit_head": chain[-1].record_hash,
                    "copied": [
                        "backtest_runs",
                        "backtest_results",
                        "backtest_trades",
                        "historical_audit_chain",
                    ],
                    "account_state_copied": False,
                }
            },
            expected_count=0,
        )
    return "PUBLISHED"
