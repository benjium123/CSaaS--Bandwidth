"""P43 (audit): which country a phone number typed WITHOUT a + belongs to.

A bare national number is ambiguous, and guessing "US" everywhere is wrong now that a
workspace can be verified in the UK: "020 7946 0958" and "07911123456" are refused outright
for a UK customer, and "2079460958" - what a spreadsheet export produces when it strips the
trunk zero - silently parses as +1 207 946 0958, a real number in Maine. A bulk import of a
UK contact list would then text strangers who never consented.

So the region comes from the workspace's own verified country (KycProfile.country), with the
platform default as the fallback for a workspace that has not been verified yet. Cached for
a short while because the send path parses numbers on every message and this value changes
about once in a company's lifetime.
"""

from __future__ import annotations

import time
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ValidationFailedError
from app.models import KycProfile

DEFAULT_REGION = "US"
CACHE_TTL_SECONDS = 300

#: org_id -> (cached_at, region, the workspace actually told us its country)
#: Per-process, like the other small caches here: forget() clears the local one only, so a
#: workspace that changes country keeps its old region on OTHER workers for up to the TTL.
#: The strict path is safe when stale (an unknown country refuses rather than guesses); a
#: stale US->GB guesses the old country for a few minutes. Bounded, and the reason the TTL
#: is minutes rather than hours.
_cache: dict[uuid.UUID, tuple[float, str, bool]] = {}


def forget(org_id: uuid.UUID | None = None) -> None:
    """Drop the cache (a workspace changed country, or a test wants a clean slate)."""
    if org_id is None:
        _cache.clear()
    else:
        _cache.pop(org_id, None)


async def _resolve(session: AsyncSession, org_id: uuid.UUID) -> tuple[str, bool]:
    hit = _cache.get(org_id)
    if hit is not None and time.monotonic() - hit[0] <= CACHE_TTL_SECONDS:
        return hit[1], hit[2]
    # JUSTIFIED allow_unscoped: called from paths that have an org_id but no bound context
    # (bulk import worker, dispatch); the filter below is on that same org_id.
    country = (
        await session.execute(
            sa.select(KycProfile.country)
            .where(KycProfile.org_id == org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    known = bool(country)
    region = (str(country).upper() if known else DEFAULT_REGION) or DEFAULT_REGION
    _cache[org_id] = (time.monotonic(), region, known)
    return region, known


async def for_org(session: AsyncSession, org_id: uuid.UUID) -> str:
    """ISO-3166 alpha-2 region to parse this workspace's bare national numbers in."""
    region, _known = await _resolve(session, org_id)
    return region


async def strict_for_org(session: AsyncSession, org_id: uuid.UUID, raw: str) -> str:
    """Region for a number where getting it wrong is worse than refusing it.

    Consent is that case: an opt-out stored against the wrong number suppresses nothing, and
    the console then shows "opted out" while the person keeps being texted. So when the
    workspace has not told us its country yet, a bare national number is REFUSED instead of
    guessed - a rejection the customer can fix beats a stored STOP that silently does
    nothing. A number written in full E.164 is unambiguous and always accepted.
    """
    region, known = await _resolve(session, org_id)
    if not known and not (raw or "").strip().startswith("+"):
        raise ValidationFailedError(
            "Include the country code (for example +447911123456), or finish verifying your "
            "business so we know which country this number is in."
        )
    return region
