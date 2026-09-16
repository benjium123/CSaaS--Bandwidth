#!/usr/bin/env python
"""Grant or revoke platform-operator access (P41). There is deliberately no API for this.

    python scripts/make_operator.py grant  someone@example.com admin
    python scripts/make_operator.py grant  someone@example.com reviewer
    python scripts/make_operator.py revoke someone@example.com

The account must already exist and should already have an authenticator app or passkey -
the operator console refuses an account without one.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.services import operators as operators_svc  # noqa: E402


async def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("grant", "revoke"):
        print(__doc__)
        return 2
    action, email = argv[0], argv[1]
    settings = load_settings()
    init_engine(settings.database_url)
    try:
        async with get_sessionmaker()() as session:
            if action == "grant":
                role = argv[2] if len(argv) > 2 else "reviewer"
                row = await operators_svc.grant(session, email=email, role=role)
                await session.commit()
                print(f"{email} is now a platform operator ({row.role})")
            else:
                await operators_svc.revoke(session, email=email)
                await session.commit()
                print(f"{email} is no longer a platform operator")
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
