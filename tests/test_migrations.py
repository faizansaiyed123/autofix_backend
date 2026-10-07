"""Migration integrity tests.

The rest of the suite builds its schema with ``Base.metadata.create_all``,
which means a broken or out-of-date Alembic revision can pass every other
test while leaving the project unable to deploy. These tests exercise the
migration chain itself on a scratch database:

1. ``upgrade head`` runs cleanly from an empty database.
2. The resulting schema matches the SQLAlchemy models (no missing revision).
3. ``downgrade base`` unwinds the whole chain.

This is the guardrail that would have caught the circular foreign key
between ``inspection_items`` and ``inspection_photos``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

# Importing every model module populates Base.metadata for the comparison.
from app import models_registry  # noqa: F401
from app.core.config import settings
from app.core.database import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRATCH_DB = "autofix_migration_check"


def _url_for(database: str) -> str:
    """Swap the database name in the configured DATABASE_URL."""
    base, _, _ = settings.DATABASE_URL.rpartition("/")
    return f"{base}/{database}"


def _alembic_config(url: str) -> Config:
    """Build an Alembic config pointed at a specific database."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
async def scratch_database() -> str:
    """Create an empty scratch database for the module, and drop it after."""
    admin_engine = create_async_engine(
        _url_for("postgres"), isolation_level="AUTOCOMMIT", poolclass=None
    )
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin_engine.dispose()

    yield _url_for(SCRATCH_DB)

    admin_engine = create_async_engine(
        _url_for("postgres"), isolation_level="AUTOCOMMIT", poolclass=None
    )
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    await admin_engine.dispose()


class TestMigrations:
    """The Alembic chain must be runnable and faithful to the models."""

    @pytest.mark.asyncio
    async def test_upgrade_head_then_matches_models_then_downgrades(
        self, scratch_database: str
    ):
        """Migrate up, assert schema parity with the models, migrate down."""
        config = _alembic_config(scratch_database)
        engine = create_async_engine(scratch_database)

        def _upgrade(sync_conn):
            config.attributes["connection"] = sync_conn
            command.upgrade(config, "head")

        def _downgrade(sync_conn):
            config.attributes["connection"] = sync_conn
            command.downgrade(config, "base")

        try:
            # 1. The whole chain applies to an empty database.
            async with engine.begin() as conn:
                await conn.run_sync(_upgrade)

            # 2. The migrated schema matches the models. A non-empty diff means
            #    a model changed without a corresponding revision.
            def _diff(sync_conn):
                context = MigrationContext.configure(
                    sync_conn,
                    opts={"compare_type": True, "include_schemas": False},
                )
                return compare_metadata(context, Base.metadata)

            async with engine.connect() as conn:
                differences = await conn.run_sync(_diff)

            unexpected = [
                d
                for d in differences
                if not (isinstance(d, tuple) and d and d[0] == "remove_index")
            ]
            assert not unexpected, (
                "Database schema does not match the SQLAlchemy models. "
                "Generate a migration with `alembic revision --autogenerate`.\n"
                f"Differences: {unexpected}"
            )

            # 3. The chain unwinds cleanly.
            async with engine.begin() as conn:
                await conn.run_sync(_downgrade)

            async with engine.connect() as conn:
                remaining = await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
                tables = [row[0] for row in remaining]
            assert tables == [], f"Tables left behind after downgrade: {tables}"
        finally:
            await engine.dispose()
