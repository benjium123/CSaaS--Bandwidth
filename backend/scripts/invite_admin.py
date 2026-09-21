#!/usr/bin/env python
"""Issue a one-time platform-operator invitation (P41 follow-up).

    python -m scripts.invite_admin someone@example.com [--expires-hours 24]

This is a trusted server-context tool. There is deliberately no public issuance API. The
printed URL contains the invitation token and is meant to be handed to the operator out of
band; it is shown once and never emailed by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.errors import CsaasError  # noqa: E402
from app.services import admin_invites as admin_invites_svc  # noqa: E402

_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_base_url(base_url: str) -> str:
    """Validate the configured base URL and return it without a trailing slash.

    Rejects anything that is not an absolute http(s) URL, anything with embedded
    credentials, a query string or a fragment, and plain http for a non-localhost host.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit(
            "public_web_url must be an absolute http(s) URL, e.g. https://app.example.com"
        )
    if parsed.username or parsed.password:
        raise SystemExit("public_web_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SystemExit("public_web_url must not contain a query string or fragment")
    if parsed.scheme != "https" and parsed.hostname not in _LOCALHOST_HOSTS:
        raise SystemExit(
            "Refusing to print an invitation over plain http for a non-localhost host. "
            "Set public_web_url to an https URL."
        )
    return base_url.rstrip("/")


def _build_invite_url(base_url: str, token: str, email: str) -> str:
    """Build the one-time signup link from an already-validated base URL."""
    return (
        f"{base_url}/admin/signup"
        f"#token={quote(token, safe='')}&email={quote(email, safe='')}"
    )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.invite_admin",
        description="Issue a one-time platform-operator invitation.",
    )
    parser.add_argument("email", help="Email address the invitation is bound to")
    parser.add_argument(
        "--expires-hours",
        type=int,
        default=24,
        help="Invitation lifetime in hours (1-168, default 24)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    # Validate the base URL BEFORE touching the database so a misconfigured
    # public_web_url cannot leave an orphan invitation behind.
    base_url = _validate_base_url(settings.public_web_url)

    init_engine(settings.database_url)
    try:
        async with get_sessionmaker()() as session:
            try:
                row, token = await admin_invites_svc.issue(
                    session, email=args.email, expires_hours=args.expires_hours
                )
            except CsaasError as exc:
                print(f"error: {exc.message}", file=sys.stderr)
                return 1
            url = _build_invite_url(base_url, token, row.email)
            await session.commit()
            print(url)
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
