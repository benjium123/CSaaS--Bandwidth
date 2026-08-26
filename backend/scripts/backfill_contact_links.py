#!/usr/bin/env python
"""Stamp pre-P2 threads with a contact.

Threads created before contacts existed have contact_id NULL. New traffic links them lazily
(the next message through a thread stamps it), but that leaves quiet threads unattributed
forever — so this runs once per deploy to catch them up.

IDEMPOTENT: re-running changes nothing. Run it after `alembic upgrade head`.

    DATABASE_URL=... python scripts/backfill_contact_links.py [--dry-run]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.models import MessageThread  # noqa: E402
from app.services.contacts import resolve_or_create_contact  # noqa: E402


async def backfill(dry_run: bool = False) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    init_engine(url)
    stamped = 0
    try:
        async with get_sessionmaker()() as session:
            # Unscoped by necessity: this is an operator task spanning every org. It only
            # ever links a thread to a contact within that thread's OWN org.
            rows = (
                await session.execute(
                    sa.select(MessageThread)
                    .where(MessageThread.contact_id.is_(None))
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            ).scalars().all()

            print(f"{len(rows)} thread(s) without a contact")
            for thread in rows:
                if dry_run:
                    print(f"  would link {thread.contact_e164} (org {thread.org_id})")
                    continue
                set_org_context(session, thread.org_id)
                contact = await resolve_or_create_contact(
                    session, thread.org_id, thread.contact_e164
                )
                thread.contact_id = contact.id
                stamped += 1
            if not dry_run:
                await session.commit()
    finally:
        await dispose_engine()

    print(f"linked {stamped} thread(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(backfill(dry_run="--dry-run" in sys.argv)))
