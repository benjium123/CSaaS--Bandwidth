#!/usr/bin/env python
"""Post-restore assertions (P14 DR-4/DR-5).

Run against a database that ``restore_drill.sh`` has just restored from the newest backup
and proven ``alembic upgrade head`` is a no-op against:

    DATABASE_URL=postgresql+asyncpg://... python scripts/smoke_restore.py

Checks, in order:
  1. ``alembic_version`` in the database matches this image's alembic head (belt and
     braces - restore_drill.sh already proved `alembic upgrade head` was a no-op, this
     confirms it from inside the app's own model of "head").
  2. Row counts: `orgs`, `users`, `roles` must be > 0 (a restore with zero orgs is not a
     real restore, it is an empty database that happens to have the right schema).
     `messages`, `calls`, `platform_events` are PRINTED but never asserted on, because a
     freshly-provisioned box may legitimately have few or none of any of them yet.
  3. A tenant-isolation spot check driven through the app's OWN session machinery
     (``app.db.base``): with an org context bound, a tenant-scoped query returns only that
     org's rows; with no context bound, the same query is refused rather than silently
     returning everyone's data. This is the thing the restore must prove above all else -
     a backup that quietly restores an isolation guard that no longer engages is worse
     than no backup at all.

Exits 0 with a report on every check; exits non-zero with a clear message on the FIRST
failure (this is a gate, not a survey).

``--self-test`` validates the pure assertion helpers below against small in-memory
fixtures. It needs no database and no DATABASE_URL - it is what proves the checks
themselves are correct, independent of any real restore.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.models import Org, OrgNumber  # noqa: E402

CRITICAL_TABLES = ("orgs", "users", "roles")
INFORMATIONAL_TABLES = ("messages", "calls", "platform_events")


class SmokeCheckFailed(RuntimeError):
    """Raised by the pure assertion helpers below - never caught except at the top level,
    so a failure always reaches the caller as a non-zero exit with a clear message."""


# ------------------------------------------------------------------------------------
# Pure, DB-free assertion helpers. Exercised directly by --self-test.
# ------------------------------------------------------------------------------------
def assert_alembic_at_head(current_rev: str | None, head_rev: str) -> None:
    if current_rev != head_rev:
        raise SmokeCheckFailed(
            f"alembic current ({current_rev!r}) != head ({head_rev!r}) - the restored "
            "database is not on the schema this image expects"
        )


def assert_positive_counts(counts: dict[str, int], required: tuple[str, ...]) -> None:
    empty = [name for name in required if counts.get(name, 0) <= 0]
    if empty:
        raise SmokeCheckFailed(
            f"expected > 0 row(s) in: {', '.join(empty)} - the restore looks empty or partial"
        )


def assert_scoped_rows_all_belong(org_id: object, rows: list) -> None:
    foreign = [r for r in rows if getattr(r, "org_id", None) != org_id]
    if foreign:
        raise SmokeCheckFailed(
            f"tenant-isolation spot check failed: {len(foreign)} row(s) returned while "
            f"scoped to org {org_id} actually belong to another org"
        )


def assert_unscoped_query_is_refused(raised: bool) -> None:
    if not raised:
        raise SmokeCheckFailed(
            "a tenant-scoped query with no org context did not raise - the isolation "
            "guard is not engaging on this database"
        )


# ------------------------------------------------------------------------------------
# DB-touching checks
# ------------------------------------------------------------------------------------
def _alembic_head(root: Path) -> str:
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise SmokeCheckFailed("this image has no alembic head at all - migrations/ is empty?")
    return head


async def _table_count(session, table: str) -> int:  # noqa: ANN001
    return (
        await session.execute(
            sa.text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed allowlist above
        )
    ).scalar_one()


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    init_engine(url)
    try:
        async with get_sessionmaker()() as session:
            expected_head = _alembic_head(ROOT)
            db_head = (
                await session.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            assert_alembic_at_head(db_head, expected_head)
            print(f"alembic       : current={db_head} head={expected_head}  OK")

            counts: dict[str, int] = {}
            for table in (*CRITICAL_TABLES, *INFORMATIONAL_TABLES):
                counts[table] = await _table_count(session, table)
            assert_positive_counts(counts, CRITICAL_TABLES)
            for table in CRITICAL_TABLES:
                print(f"{table:<15}: {counts[table]} row(s)  OK")
            for table in INFORMATIONAL_TABLES:
                print(f"{table:<15}: {counts[table]} row(s)  (informational, not asserted)")

            org_row = (
                await session.execute(
                    sa.select(Org.id)
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                    .limit(1)
                )
            ).first()
            if org_row is None:
                print("tenant scope  : SKIPPED (no orgs in the restored data)")
            else:
                org_id = org_row[0]
                set_org_context(session, org_id)
                scoped = list((await session.execute(sa.select(OrgNumber))).scalars().all())
                assert_scoped_rows_all_belong(org_id, scoped)
                print(
                    f"tenant scope  : org {org_id} -> {len(scoped)} org_numbers, "
                    "all correctly scoped  OK"
                )

                # The other half of the guarantee: no context bound must FAIL CLOSED,
                # never silently return every org's rows.
                set_org_context(session, None)
                raised = False
                try:
                    await session.execute(sa.select(OrgNumber))
                except Exception:
                    raised = True
                assert_unscoped_query_is_refused(raised)
                print("tenant scope  : unscoped query correctly refused  OK")
    except SmokeCheckFailed as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print("\nsmoke_restore: all checks passed")
    return 0


def _self_test() -> int:
    """DB-free: exercises every assertion helper's pass AND fail path."""
    from dataclasses import dataclass

    @dataclass
    class Row:
        org_id: object

    assert_alembic_at_head("abc123", "abc123")
    try:
        assert_alembic_at_head("abc123", "def456")
    except SmokeCheckFailed:
        pass
    else:
        raise AssertionError("assert_alembic_at_head did not raise on a mismatch")

    assert_positive_counts({"orgs": 1, "users": 2, "roles": 3}, CRITICAL_TABLES)
    try:
        assert_positive_counts({"orgs": 0, "users": 2, "roles": 3}, CRITICAL_TABLES)
    except SmokeCheckFailed:
        pass
    else:
        raise AssertionError("assert_positive_counts did not raise on a zero-row table")

    org_a, org_b = object(), object()
    assert_scoped_rows_all_belong(org_a, [Row(org_a), Row(org_a)])
    try:
        assert_scoped_rows_all_belong(org_a, [Row(org_a), Row(org_b)])
    except SmokeCheckFailed:
        pass
    else:
        raise AssertionError("assert_scoped_rows_all_belong did not raise on a foreign row")

    try:
        assert_unscoped_query_is_refused(False)
    except SmokeCheckFailed:
        pass
    else:
        raise AssertionError("assert_unscoped_query_is_refused did not raise when nothing raised")
    assert_unscoped_query_is_refused(True)  # must not raise

    print("smoke_restore --self-test: all assertion helpers behave correctly")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    raise SystemExit(asyncio.run(main()))
