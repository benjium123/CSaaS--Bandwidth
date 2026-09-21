"""Migration 0059 adds orgs.kyc_required: new orgs are gated, existing ones grandfathered.

Open registration mints individual workspaces with no operator in the loop, so an org
created after 0059 starts with ``kyc_required`` true and must clear identity verification
before it transacts. Orgs that already existed when the migration ran are grandfathered:
the column is added with a server default of false (which backfills them) and only then is
the server default switched to true, so future rows are gated.

This drives the executed DDL - not ``Base.metadata`` - against an isolated in-memory
SQLite database holding one legacy row, using the same importlib + real ``Operations``
harness as ``test_individual_signup.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_BACKEND = Path(__file__).resolve().parent.parent
MIGRATION_0059 = (
    _BACKEND / "migrations" / "versions" / "0059_org_kyc_required.py"
)


def _load_migration_0059():
    spec = importlib.util.spec_from_file_location("_migration_0059", MIGRATION_0059)
    assert spec is not None and spec.loader is not None, MIGRATION_0059
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration_step(module, name: str, ctx) -> None:
    """Run ``module.<name>()`` with a real Operations installed as ``alembic.op``.

    ``Operations.context`` installs the module-level ``op`` proxy for the duration of the
    block, so the revision's ``op.*`` calls dispatch to the Operations bound to this
    connection.
    """
    with Operations.context(ctx):
        getattr(module, name)()


def _kyc_required(conn, org_id: str) -> bool:
    """Read the raw column; SQLite stores booleans as 0/1, so normalise to a bool."""
    value = conn.execute(
        sa.text("SELECT kyc_required FROM orgs WHERE id = :id"), {"id": org_id}
    ).scalar_one()
    assert value is not None, org_id
    return bool(value)


def test_migration_0059_kyc_required_upgrade_then_downgrade():
    module = _load_migration_0059()

    engine = sa.create_engine("sqlite://")
    try:
        metadata = sa.MetaData()
        sa.Table(
            "orgs",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(63), nullable=False),
        )
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO orgs (id, name, slug) "
                    "VALUES ('legacy', 'Legacy Co', 'legacy-co')"
                )
            )

        with engine.begin() as conn:
            _run_migration_step(module, "upgrade", MigrationContext.configure(conn))

        with engine.begin() as conn:
            # Grandfathered: the row predates the policy, so it takes the default false.
            assert _kyc_required(conn, "legacy") is False

            # A row inserted without the column takes the *new* server default true.
            conn.execute(
                sa.text(
                    "INSERT INTO orgs (id, name, slug) "
                    "VALUES ('new', 'New Co', 'new-co')"
                )
            )
            assert _kyc_required(conn, "new") is True

        with engine.begin() as conn:
            _run_migration_step(module, "downgrade", MigrationContext.configure(conn))

        with engine.begin() as conn:
            columns = {
                row[1] for row in conn.execute(sa.text("PRAGMA table_info(orgs)"))
            }
            assert "kyc_required" not in columns
            # Both rows survive the downgrade; the legacy one is untouched.
            assert conn.execute(sa.text("SELECT COUNT(*) FROM orgs")).scalar_one() == 2
            assert (
                conn.execute(
                    sa.text("SELECT name FROM orgs WHERE id = 'legacy'")
                ).scalar_one()
                == "Legacy Co"
            )
    finally:
        engine.dispose()
