"""Alembic environment — DB-003.

The database URL always comes from application settings, never from
``alembic.ini``: a migration run must target exactly the database the
application targets, and duplicating the URL in two places is how a migration
ends up applied to the wrong one.

Importing ``app.db.models`` registers every model on the shared metadata, which
is what autogenerate diffs against.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.models import metadata as target_metadata  # noqa: F401 - registers models

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would switch off every
    # ``ats.*`` logger already configured in this process. The entrypoint runs
    # migrations before starting the API, so that default would silently kill
    # application logging for the life of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Timescale creates internal chunk tables under these schemas; without
        # this filter autogenerate would try to "drop" them on every run.
        include_schemas=False,
        version_table="alembic_version",
    )


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
