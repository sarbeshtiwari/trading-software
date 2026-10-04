"""Copy only isolated fixture state into a disposable PostgreSQL recovery database."""

import os
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.validation import _utc
from app.db import session as db_session
from app.db.models import metadata
from tests.integration.test_fresh_migrations import run_alembic


@asynccontextmanager
async def process_database(settings, storage, monkeypatch):
    if storage == "sqlite":
        yield
        return
    configured = make_url(os.environ["ATS_TEST_POSTGRES_URL"])
    database = "ats_recovery_test_" + uuid4().hex
    assert database != configured.database
    admin = create_async_engine(configured, isolation_level="AUTOCOMMIT")
    target = configured.set(database=database)
    engine = None
    created = False
    try:
        snapshots = {}
        async with db_session.session_scope() as session:
            for table in metadata.sorted_tables:
                snapshots[table.name] = [
                    {key: _utc(value) if isinstance(value, datetime) else value
                     for key, value in row.items()}
                    for row in (await session.execute(sa.select(table))).mappings()
                ]
        async with admin.connect() as connection:
            await connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
            created = True
        await run_alembic(target, "upgrade", "head")
        engine = create_async_engine(target, **db_session._engine_kwargs(
            settings.model_copy(update={"database_url": target.render_as_string(hide_password=False)})
        ))
        db_session.install_connection_cleanup(engine)
        async with engine.begin() as connection:
            for table in metadata.sorted_tables:
                if snapshots[table.name]:
                    await connection.execute(sa.insert(table), snapshots[table.name])
        with monkeypatch.context() as scoped:
            scoped.setattr(settings, "database_url", target.render_as_string(hide_password=False))
            scoped.setattr(db_session, "_sessionmaker", async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            ))
            yield
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(sa.text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()
