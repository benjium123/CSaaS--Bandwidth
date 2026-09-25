"""Give every org a one-time launch credit when billing v2 switches the prepaid gate on.

Idempotent: the ledger reference is fixed per org, so running it twice credits once.
Run inside the api container:
    docker exec -i csaas-api-1 python scripts/launch_credit.py            # dry run
    docker exec -i csaas-api-1 python scripts/launch_credit.py --apply    # credit $20 each
    docker exec -i csaas-api-1 python scripts/launch_credit.py --apply --dollars 50
"""

from __future__ import annotations

import argparse
import asyncio

import sqlalchemy as sa

from app.config import load_settings
from app.db.base import set_org_context
from app.db.session import get_sessionmaker, init_engine
from app.models import Org
from app.services import credits

REFERENCE = "launch-credit-billing-v2"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dollars", type=int, default=20)
    args = ap.parse_args()
    init_engine(load_settings().database_url)
    async with get_sessionmaker()() as session:
        orgs = (await session.execute(sa.select(Org).order_by(Org.created_at))).scalars().all()
        for org in orgs:
            set_org_context(session, org.id)
            before = await credits.balance(session, org.id)
            if not args.apply:
                print(f"would credit ${args.dollars} to {org.name} ({org.id}); balance {before}")
                continue
            entry = await credits.adjust(
                session,
                org.id,
                args.dollars * 1_000_000,
                reference=f"{REFERENCE}:{org.id}",
                note="Launch credit (billing v2 prepaid switch-on)",
                created_by=None,
            )
            await session.commit()
            print(f"{org.name}: balance {before} -> {entry.balance_after_micros}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
