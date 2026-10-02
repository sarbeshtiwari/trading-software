"""Install and compare migrations on a disposable database in the existing server."""

import asyncio
import os
import sys
from urllib.parse import quote
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import BACKEND_ROOT

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL not configured")


async def run_alembic(url, *arguments, expect_success=True):
    rendered = url.render_as_string(hide_password=False)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        cwd=BACKEND_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": rendered,
            "TRADING_MODE": "PAPER",
            "BROKER_PROVIDER": "paper",
            "PAPER_WORKER_ENABLED": "false",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        output = (stdout + stderr).decode(errors="replace").replace(rendered, "<database URL>")
        if url.password:
            output = output.replace(url.password, "<password>").replace(
                quote(url.password, safe=""), "<password>"
            )
        assert (process.returncode == 0) == expect_success, output
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_empty_postgres_upgrade_and_metadata_check():
    configured = make_url(POSTGRES_URL)
    database = "ats_migration_test_" + uuid4().hex
    assert database != configured.database
    admin = create_async_engine(configured, isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
            created = True
        target = configured.set(database=database)
        await run_alembic(target, "upgrade", "head")
        await run_alembic(target, "check")
        await run_alembic(target, "downgrade", "0015_serialized_fill_limits")
        await run_alembic(target, "upgrade", "head")
        await run_alembic(target, "check")
        isolated = create_async_engine(target)
        try:
            async with isolated.begin() as connection:
                await connection.execute(
                    sa.text("ALTER TABLE instruments ADD COLUMN unexpected_test_column TEXT")
                )
            output = await run_alembic(target, "check", expect_success=False)
            assert "unexpected_test_column" in output
        finally:
            await isolated.dispose()
    finally:
        if created:
            async with admin.connect() as connection:
                await connection.execute(sa.text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()
