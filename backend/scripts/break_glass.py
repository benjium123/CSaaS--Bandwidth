#!/usr/bin/env python
"""H4 break-glass: give an existing account temporary ADMIN operator access (max 60 min).

    python scripts/break_glass.py someone@example.com "all admins locked out" [minutes]

Run on the server only; there is no API for this. The account must already exist and still
needs its own passkey or authenticator app to use the console. Every active admin is emailed,
a security alert is opened and the grant is in the operator audit log. Access ends by itself.
"""

from __future__ import annotations

import asyncio
import getpass
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.services import break_glass  # noqa: E402


async def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    email, reason = argv[0], argv[1]
    minutes = int(argv[2]) if len(argv) > 2 else 60
    settings = load_settings()
    init_engine(settings.database_url)
    try:
        async with get_sessionmaker()() as session:
            row = await break_glass.grant(
                session,
                settings,
                email=email,
                reason=reason,
                minutes=minutes,
                granted_by=f"{getpass.getuser()}@{socket.gethostname()}",
            )
            print(f"{email} is an ADMIN operator until {row.expires_at.isoformat()} (UTC)")
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
