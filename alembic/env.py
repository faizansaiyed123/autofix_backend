"""Alembic environment configuration.

Supports three modes:

* offline  - render migrations as SQL without a database connection
* online   - open an async engine and migrate (the normal CLI path)
* embedded - reuse a connection supplied by the caller through
             ``config.attributes["connection"]``, which is how the test
             suite drives migrations from inside a running event loop

The database URL comes from ``settings`` unless the caller overrode
``sqlalchemy.url`` on the Alembic config.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection

from alembic import context

# Set up sys.path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Importing the registry populates Base.metadata with every model.
from app import models_registry  # noqa: F401
from app.core.config import settings
from app.core.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_async_url() -> str:
    """Async database URL, honouring an explicit config override."""
    return config.get_main_option("sqlalchemy.url") or settings.DATABASE_URL


def get_sync_url() -> str:
    """Synchronous database URL for offline mode."""
    return get_async_url().replace("+asyncpg", "")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL scripts)."""
    context.configure(
        url=get_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against an open synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        include_schemas=False,
        version_table_schema=None,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open an async engine and run migrations."""
    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(get_async_url(), poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # A caller (such as the test suite) may hand us an already-open sync
    # connection. Reusing it avoids nesting asyncio.run() inside a running loop.
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        do_run_migrations(existing_connection)
    else:
        asyncio.run(run_migrations_online())
