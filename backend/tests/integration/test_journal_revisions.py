"""Append-only journal versions cannot replace or multiply execution evidence."""

import asyncio

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.audit.service import AuditService
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.journal import JournalAnnotation, JournalEntry, JournalRevision
from app.modes import TradingMode
from app.portfolio.journal_integrity import journal_verified
from app.strategies.paper_evidence import trade_blocker
from tests.conftest import BACKEND_ROOT
from tests.integration.test_journal_api import credentials, journal_client, negative

__all__ = ["credentials", "journal_client"]
BODY = {
    "expected_version": 1,
    "rejection_detail": "Owner contextual review; no order was sent",
    "reason": "Reviewed recorded validation failure",
}


async def identifier(client, fake_clock):
    await negative(fake_clock)
    return (await client.get("/api/v1/journal")).json()["entries"][0]["id"]


async def test_core_mutations_rejected_but_annotations_remain_appendable(
    journal_client, fake_clock
):
    root_id = await identifier(journal_client, fake_clock)
    with pytest.raises(ValueError, match="append-only"):
        async with db_session.session_scope() as session:
            row = await session.get(JournalEntry, root_id)
            row.rejection_code = "REWRITTEN"
    with pytest.raises(ValueError, match="append-only"):
        async with db_session.session_scope() as session:
            await session.delete(await session.get(JournalEntry, root_id))
    for statement in (
        sa.update(JournalEntry).values(rejection_code="BULK_REWRITE"),
        sa.delete(JournalEntry),
    ):
        with pytest.raises(ValueError, match="bulk mutations"):
            async with db_session.session_scope() as session:
                await session.execute(statement)
    response = await journal_client.post(
        f"/api/v1/journal/{root_id}/annotations",
        json={
            "note": "Owner review",
            "reason": "Annotations remain appendable",
        },
    )
    assert response.status_code == 201, response.text


async def test_corrections_keep_original_seal_and_link_all_versions(journal_client, fake_clock):
    root_id = await identifier(journal_client, fake_clock)
    original = (await journal_client.get(f"/api/v1/journal/{root_id}")).json()["entry"]
    response = await journal_client.post(f"/api/v1/journal/{root_id}/corrections", json=BODY)
    assert response.status_code == 201, response.text
    detail = response.json()
    second = detail["entry"]
    assert second["version"] == 2 and second["id"] != root_id
    assert second["record_role"] == "OWNER_CONTEXT_CORRECTION"
    assert second["previous_version_id"] == second["revision_root_id"] == root_id
    assert second["rejection_code"] == original["rejection_code"]
    assert second["rejection_detail"] == BODY["rejection_detail"]
    assert second["net_pnl"] is None and second["quantity"] is None
    assert detail["revisions"][0]["actor"] == "owner"
    third_response = await journal_client.post(
        f"/api/v1/journal/{second['id']}/corrections",
        json={
            **BODY,
            "expected_version": 2,
            "rejection_detail": "Owner further contextual review",
        },
    )
    assert third_response.status_code == 201, third_response.text
    third = third_response.json()["entry"]
    assert third["version"] == 3 and third["previous_version_id"] == second["id"]
    reloaded = (await journal_client.get(f"/api/v1/journal/{root_id}")).json()
    assert reloaded["entry"] | {"latest_version_id": root_id} == original
    assert len(reloaded["revisions"]) == 2
    assert reloaded["entry"]["latest_version_id"] == third["id"]
    async with db_session.session_scope() as session:
        for entry_id in (root_id, second["id"], third["id"]):
            assert await journal_verified(session, entry_id)
            row = await session.get(JournalEntry, entry_id)
            assert (
                await trade_blocker(session, row, None, None, (), as_of=fake_clock.now())
                == "JOURNAL_CORRECTION_REQUIRES_REVIEW"
            )
    stale = await journal_client.post(f"/api/v1/journal/{root_id}/corrections", json=BODY)
    assert stale.status_code == 409
    forbidden = await journal_client.post(
        f"/api/v1/journal/{third['id']}/corrections",
        json={
            **BODY,
            "expected_version": 3,
            "net_pnl": "100000",
        },
    )
    assert forbidden.status_code == 422
    workspace = (await journal_client.get("/api/v1/workspace")).json()
    assert [row["id"] for row in workspace["journal"]] == [root_id]


async def test_concurrent_revision_attempts_have_one_winner(journal_client, fake_clock):
    root_id = await identifier(journal_client, fake_clock)
    responses = await asyncio.gather(
        *(
            journal_client.post(f"/api/v1/journal/{root_id}/corrections", json=BODY)
            for _attempt in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 2


async def test_correction_audit_failure_rolls_back_whole_version(
    journal_client, fake_clock, monkeypatch
):
    root_id = await identifier(journal_client, fake_clock)
    original = AuditService.append_in_session

    async def fail(self, session, identity, *args, **kwargs):
        if identity.event_type == "JOURNAL_CORRECTED":
            raise ValueError("injected correction audit failure")
        return await original(self, session, identity, *args, **kwargs)

    monkeypatch.setattr(AuditService, "append_in_session", fail)
    response = await journal_client.post(f"/api/v1/journal/{root_id}/corrections", json=BODY)
    assert response.status_code == 409
    async with db_session.session_scope() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalRevision)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 1
        assert await journal_verified(session, root_id)


async def test_revision_deletion_detected_by_read_export_and_evidence(
    db_engine, journal_client, fake_clock
):
    root_id = await identifier(journal_client, fake_clock)
    assert (
        await journal_client.post(f"/api/v1/journal/{root_id}/corrections", json=BODY)
    ).status_code == 201
    async with db_engine.begin() as connection:
        await connection.execute(sa.delete(JournalRevision))
    for path in ("", f"/{root_id}", "/export/json"):
        assert (await journal_client.get(f"/api/v1/journal{path}")).status_code == 409
    async with db_session.session_scope() as session:
        row = await session.get(JournalEntry, root_id)
        assert (
            await trade_blocker(session, row, None, None, (), as_of=fake_clock.now())
            == "JOURNAL_CORRECTION_REQUIRES_REVIEW"
        )


def test_migrated_sqlite_refuses_raw_mutation_and_downgrades(settings_env, tmp_path):
    path = tmp_path / "journal-migration.db"
    settings_env(DATABASE_URL=f"sqlite+aiosqlite:///{path}")
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(JournalEntry),
                [
                    {"id": "isolated-migration-journal", "mode": TradingMode.PAPER},
                    {"id": "isolated-migration-revision", "mode": TradingMode.PAPER},
                ],
            )
            connection.execute(
                sa.insert(JournalAnnotation),
                {
                    "id": "isolated-migration-note",
                    "journal_entry_id": "isolated-migration-journal",
                    "note": "Isolated migration fixture",
                },
            )
            connection.execute(
                sa.insert(JournalRevision),
                {
                    "entry_id": "isolated-migration-revision",
                    "previous_id": "isolated-migration-journal",
                    "root_id": "isolated-migration-journal",
                    "version": 2,
                    "actor": "isolated-test",
                    "reason": "Isolated migration fixture",
                    "changes": {},
                    "occurred_at": get_clock().utcnow(),
                },
            )
            triggers = (
                connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                .scalars()
                .all()
            )
            assert all(
                f"{table}_immutable_{action}" in triggers
                for table in ("journal_entries", "journal_annotations", "journal_revisions")
                for action in ("update", "delete")
            )
        for sql in (
            "UPDATE journal_entries SET net_pnl = 100",
            "DELETE FROM journal_entries",
            "UPDATE journal_annotations SET note = 'altered'",
            "DELETE FROM journal_annotations",
            "UPDATE journal_revisions SET actor = 'altered'",
            "DELETE FROM journal_revisions",
        ):
            with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                with engine.begin() as connection:
                    connection.exec_driver_sql(sql)
        command.downgrade(config, "0009_walkforward_jobs")
        with engine.begin() as connection:
            assert "journal_revisions" not in sa.inspect(connection).get_table_names()
            assert connection.scalar(sa.select(sa.func.count()).select_from(JournalEntry)) == 2
    finally:
        engine.dispose()
