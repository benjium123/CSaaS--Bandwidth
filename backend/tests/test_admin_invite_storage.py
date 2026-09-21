"""Real-Postgres atomicity of admin-invite consumption, plus a migration roundtrip.

WHY THIS FILE EXISTS. `app.services.admin_invites.consume` is the only place an admin
invitation is redeemed, and its correctness rests on a single guarded UPDATE ... RETURNING
that must let exactly one of two racing callers win. The suite's default database does not
exercise that guard under real concurrency, so the concurrency test spins its own embedded
Postgres, exactly as `test_migrations_build_the_database.py` does, but on a private data
directory and database name so the two files never share state.

The migration test is deliberately separate and synchronous: it loads
`migrations/versions/0060_admin_invites.py` by path, runs it against a throwaway SQLite
engine, and checks that the table appears, accepts a GUID bind, and disappears on
downgrade without disturbing pre-existing rows.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.errors import ValidationFailedError
from app.models.admin_invite import AdminInvite
from app.services import admin_invites

BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: Private to this file. The migration harness next door uses `csaas_migration_check` /
#: `migration_check`; sharing either would make the two files order-dependent.
PG_DATA_DIR = "csaas_admin_entry_pg_test"
PG_DATABASE = "admin_invite_check"


@pytest.fixture(scope="module")
def invite_database() -> str:
    """A fresh Postgres database with `alembic upgrade head` run against it.

    Alembic runs in a subprocess for the same reason as the migration harness: `env.py`
    builds its own event loop, and calling it in-process from an async test means two
    loops fighting over one thread.
    """
    pgserver = pytest.importorskip(
        "pgserver", reason="embedded Postgres is needed to test atomic consumption"
    )

    data = pathlib.Path(os.environ.get("TEMP", "/tmp")) / PG_DATA_DIR
    server = pgserver.get_server(str(data), cleanup_mode="stop")
    server.psql(f"DROP DATABASE IF EXISTS {PG_DATABASE};")
    server.psql(f"CREATE DATABASE {PG_DATABASE};")
    base = server.get_uri().rsplit("/", 1)[0]
    url = base.replace("postgresql://", "postgresql+asyncpg://") + f"/{PG_DATABASE}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, (
        "alembic upgrade head FAILED against the invite test database.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return url


async def _issue(url: str, email: str) -> tuple[uuid.UUID, str]:
    """Issue an invitation in its own session and commit it."""
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row, token = await admin_invites.issue(session, email=email, expires_hours=24)
            await session.commit()
            return row.id, token
    finally:
        await engine.dispose()


async def test_two_sessions_racing_consume_exactly_one_wins(invite_database) -> None:
    """The guarded UPDATE must let exactly one of two concurrent consumers through."""
    email = "race@example.com"
    invite_id, token = await _issue(invite_database, email)

    engine = create_async_engine(invite_database)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def attempt() -> str:
            async with factory() as session:
                try:
                    row = await admin_invites.consume(session, token=token, email=email)
                    await session.commit()
                    assert row.consumed_at is not None
                    return "won"
                except ValidationFailedError as exc:
                    assert exc.code == "invalid_admin_invite"
                    await session.rollback()
                    return "lost"

        results = await asyncio.wait_for(
            asyncio.gather(attempt(), attempt()), timeout=30
        )

        assert sorted(results) == ["lost", "won"], results

        async with factory() as session:
            stored = await session.get(AdminInvite, invite_id)
            assert stored is not None
            assert stored.consumed_at is not None
    finally:
        await engine.dispose()


async def test_rollback_permits_a_second_acceptance(invite_database) -> None:
    """A consumer that rolls back must leave the invitation usable by the next caller."""
    email = "rollback@example.com"
    _, token = await _issue(invite_database, email)

    engine = create_async_engine(invite_database)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session_a:
            row = await admin_invites.consume(session_a, token=token, email=email)
            assert row.consumed_at is not None
            await session_a.rollback()

        async with factory() as session_b:
            row = await admin_invites.consume(session_b, token=token, email=email)
            await session_b.commit()
            assert row.consumed_at is not None
            assert row.consumed_by is None
    finally:
        await engine.dispose()


async def test_consume_rejects_an_unknown_token(invite_database) -> None:
    engine = create_async_engine(invite_database)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(ValidationFailedError) as excinfo:
                await admin_invites.consume(
                    session, token="not-a-real-token", email="nobody@example.com"
                )
            assert excinfo.value.code == "invalid_admin_invite"
            await session.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Migration roundtrip, on SQLite, synchronous.
# ---------------------------------------------------------------------------------------

MIGRATION_PATH = BACKEND / "migrations" / "versions" / "0060_admin_invites.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0060_admin_invites", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_invites_migration_roundtrip() -> None:
    """Upgrade creates the table and accepts a GUID bind; downgrade removes it and leaves
    pre-existing rows in `users` untouched."""
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.execute(
                sa.text(
                    "CREATE TABLE users (id CHAR(36) NOT NULL PRIMARY KEY, email VARCHAR(320))"
                )
            )
            user_id = uuid.uuid4()
            conn.execute(
                sa.text("INSERT INTO users (id, email) VALUES (:id, :email)"),
                {"id": str(user_id), "email": "existing@example.com"},
            )

        migration = _load_migration()

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        inspector = sa.inspect(engine)
        assert "admin_invites" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("admin_invites")}
        assert columns == {
            "id",
            "email",
            "token_hash",
            "expires_at",
            "consumed_at",
            "consumed_by",
            "issued_via",
            "created_at",
            "updated_at",
        }

        # A GUID bind must round-trip through the portable type on SQLite.
        invite_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO admin_invites "
                    "(id, email, token_hash, expires_at, consumed_by, issued_via, "
                    " created_at, updated_at) "
                    "VALUES (:id, :email, :token_hash, :expires_at, :consumed_by, "
                    "        :issued_via, :created_at, :updated_at)"
                ),
                {
                    "id": str(invite_id),
                    "email": "bound@example.com",
                    "token_hash": "a" * 64,
                    "expires_at": now + timedelta(hours=24),
                    "consumed_by": str(user_id),
                    "issued_via": "trusted_cli",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            stored = conn.execute(
                sa.text("SELECT consumed_by FROM admin_invites WHERE id = :id"),
                {"id": str(invite_id)},
            ).scalar_one()
        assert uuid.UUID(str(stored)) == user_id

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

        inspector = sa.inspect(engine)
        assert "admin_invites" not in inspector.get_table_names()
        with engine.connect() as conn:
            remaining = conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
        assert remaining == 1
    finally:
        engine.dispose()


def test_admin_invite_model_accepts_a_guid_bind() -> None:
    """The ORM model must be usable against the migrated SQLite schema, including the
    GUID-typed foreign key, so the migration and the model agree on the wire format."""
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.execute(
                sa.text(
                    "CREATE TABLE users (id CHAR(36) NOT NULL PRIMARY KEY, email VARCHAR(320))"
                )
            )

        migration = _load_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        user_id = uuid.uuid4()
        with Session(engine) as session:
            session.execute(
                sa.text("INSERT INTO users (id, email) VALUES (:id, :email)"),
                {"id": str(user_id), "email": "fk@example.com"},
            )
            invite = AdminInvite(
                email="model@example.com",
                token_hash="b" * 64,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                consumed_by=user_id,
            )
            session.add(invite)
            session.flush()
            assert isinstance(invite.id, uuid.UUID)
            assert invite.consumed_by == user_id
            session.rollback()
    finally:
        engine.dispose()
