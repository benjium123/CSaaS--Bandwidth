"""Do the migrations actually build the schema the code expects?

WHY THIS FILE EXISTS. `tests/conftest.py` builds its tables with `Base.metadata.create_all`,
and nothing in the suite runs alembic - `grep -rln alembic tests/` found only the graph test
next door, which validates the revision GRAPH and never executes a migration. So until now
the suite carried **zero information about whether the database can be built**: a column
could exist in a model and in no migration, every test would pass, and the first execution of
any new migration would be a real deployment.

`tests/test_migration_history.py` answers "is the chain well formed". This answers "does
running it produce the schema the models describe", which is the question that actually fails
a deploy.

Embedded Postgres rather than the suite's SQLite, because that is what production runs and
because several migrations use Postgres-only DDL. Skipped, not failed, when `pgserver` is not
installed - a developer without it should not be blocked, but CI should have it.

The comparison is deliberately NAMES ONLY - tables and columns. Comparing types would fail on
legitimate differences between what a migration declares and what SQLAlchemy reflects back,
and a type mismatch is a much rarer mistake than a forgotten column.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 - populates the metadata this test compares against
from app.db.base import Base

BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: Tables alembic itself owns, which are not in the application's metadata.
ALEMBIC_TABLES = {"alembic_version"}


@pytest.fixture(scope="module")
def migrated_database() -> str:
    """A fresh Postgres database with `alembic upgrade head` run against it.

    Alembic runs in a SUBPROCESS on purpose. `migrations/env.py` builds its own asyncio event
    loop, and calling it in-process from an async test means two loops fighting over one
    thread - a failure that looks like a migration bug and is not one.
    """
    pgserver = pytest.importorskip(
        "pgserver", reason="embedded Postgres is needed to run the migrations"
    )

    data = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "csaas_migration_check"
    server = pgserver.get_server(str(data), cleanup_mode="stop")
    server.psql("DROP DATABASE IF EXISTS migration_check;")
    server.psql("CREATE DATABASE migration_check;")
    base = server.get_uri().rsplit("/", 1)[0]
    url = base.replace("postgresql://", "postgresql+asyncpg://") + "/migration_check"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        "alembic upgrade head FAILED - the migrations cannot build the database.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return url


async def _reflect(url: str) -> dict[str, set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
            return {
                name: await conn.run_sync(
                    lambda c, n=name: {col["name"] for col in sa.inspect(c).get_columns(n)}
                )
                for name in tables
            }
    finally:
        await engine.dispose()


async def test_the_migrations_create_every_table_the_models_declare(migrated_database) -> None:
    built = await _reflect(migrated_database)
    missing = set(Base.metadata.tables) - set(built)
    assert not missing, (
        f"{len(missing)} table(s) exist in the models and in no migration: {sorted(missing)}. "
        "The suite would still be green: conftest builds tables from metadata."
    )


async def test_every_model_column_exists_in_the_built_schema(migrated_database) -> None:
    """The likelier drift than a whole missing table: a column added to a model with no
    migration written for it. Invisible to a metadata-built test database by construction."""
    built = await _reflect(migrated_database)
    problems: list[str] = []
    for name, table in Base.metadata.tables.items():
        if name not in built:
            continue  # reported by the test above; not repeated here
        missing = {c.name for c in table.columns} - built[name]
        if missing:
            problems.append(f"{name}: {sorted(missing)}")
    assert not problems, "columns in the models that no migration creates:\n" + "\n".join(
        problems
    )


async def test_the_built_schema_has_no_tables_the_models_have_forgotten(
    migrated_database,
) -> None:
    """The other direction, and a weaker claim on purpose: a table a migration creates that
    no model describes is usually dead weight from a removed feature rather than a fault, so
    this reports rather than forbids - it fails only so that the list gets read once."""
    built = await _reflect(migrated_database)
    orphans = set(built) - set(Base.metadata.tables) - ALEMBIC_TABLES
    assert not orphans, (
        f"table(s) built by migrations with no model: {sorted(orphans)}. If that is "
        "deliberate, add them to this test's allowlist with the reason."
    )
