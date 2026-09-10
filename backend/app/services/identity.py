"""P25 enterprise identity: sessions, login history, and org security helpers.

Sessions and LoginEvents are NOT TenantScoped (see ``app/models/identity.py``). Every
query here therefore filters explicitly by user_id and/or org_id; never a bare
``sa.func.count()`` — always ``sa.func.count(Model.id)``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models import LOGIN_OUTCOMES, LoginEvent, Org, User
from app.models import Session as IdentitySession


def client_ip(request: Request) -> str | None:
    """First hop of X-Forwarded-For, otherwise the socket peer address."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first[:64]

    client = request.client
    if client is not None and client.host:
        return client.host[:64]

    return None


def client_user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    if not ua:
        return None
    return ua[:255]


async def create_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID | None = None,
    request: Request,
    expire_hours: int,
) -> IdentitySession:
    """Build a live Session row and add it to the caller's session. No commit here."""
    now = datetime.now(timezone.utc)
    row = IdentitySession(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
        last_seen_at=now,
        expires_at=now + timedelta(hours=expire_hours),
    )
    session.add(row)
    return row


def record_login_event(
    session: AsyncSession,
    *,
    email: str,
    outcome: str,
    user_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    request: Request | None = None,
    detail: str | None = None,
) -> LoginEvent:
    """Record a login outcome. No commit here. `detail` must never carry a password or token."""
    if outcome not in LOGIN_OUTCOMES:
        raise ValidationFailedError(f"Unknown login outcome: {outcome}")

    row = LoginEvent(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        email=email,
        at=datetime.now(timezone.utc),
        ip=client_ip(request) if request is not None else None,
        user_agent=client_user_agent(request) if request is not None else None,
        outcome=outcome,
        detail=(detail or None) if detail is None else detail[:255],
    )
    session.add(row)
    return row


def _datetime_future(value: datetime, now: datetime | None = None) -> bool:
    """Naive/aware safe expiry comparison (SQLite returns naive datetimes)."""
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > current


async def get_live_session(session: AsyncSession, sid: uuid.UUID) -> IdentitySession | None:
    row = await session.get(IdentitySession, sid)
    if row is None or row.revoked_at is not None:
        return None
    if not _datetime_future(row.expires_at):
        return None
    return row


async def list_sessions_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 100
) -> list[IdentitySession]:
    # SQLite stores timezone=True columns as naive datetimes, so the expiry filter is
    # applied in Python to avoid a naive-vs-aware SQL comparison silently missing rows.
    stmt = (
        sa.select(IdentitySession)
        .where(IdentitySession.user_id == user_id)
        .order_by(IdentitySession.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    live_rows = [row for row in rows if _datetime_future(row.expires_at)]
    return live_rows[:limit]


async def revoke_session(
    session: AsyncSession, row: IdentitySession, *, revoked_by: uuid.UUID
) -> None:
    row.revoked_at = datetime.now(timezone.utc)
    row.revoked_by = revoked_by


async def revoke_all_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    revoked_by: uuid.UUID,
    keep_sid: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Revoke every live session for a user, returning the affected session ids.

    Callers need the ids so they can ``session_cache.mark_revoked`` each one.
    """
    conditions = [
        IdentitySession.user_id == user_id,
        IdentitySession.revoked_at.is_(None),
    ]
    if keep_sid is not None:
        conditions.append(IdentitySession.id != keep_sid)

    stmt = sa.select(IdentitySession.id).where(*conditions)
    ids = list((await session.execute(stmt)).scalars().all())
    if not ids:
        return ids

    # synchronize_session=False: the ids were just selected and the caller commits
    # straight after, so paying for identity-map synchronisation buys nothing and the
    # 'evaluate' strategy would have to interpret the IN clause row by row.
    await session.execute(
        sa.update(IdentitySession)
        .where(IdentitySession.id.in_(ids), IdentitySession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc), revoked_by=revoked_by)
        .execution_options(synchronize_session=False)
    )
    return ids


def parse_cidrs(values: object) -> list[str]:
    """Validate and normalise an org IP allowlist."""
    if not isinstance(values, list):
        raise ValidationFailedError("ip_allowlist must be a list of CIDR strings")
    if len(values) > 100:
        raise ValidationFailedError("ip_allowlist supports at most 100 CIDR entries")

    parsed: list[str] = []
    for entry in values:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except (ValueError, TypeError) as exc:
            raise ValidationFailedError(f"Invalid CIDR in allowlist: {entry}") from exc
        parsed.append(str(network))
    return parsed


def ip_in_allowlist(ip: str | None, allowlist: list | None) -> bool:
    """Return True for no restriction; fail CLOSED for unknown/unparseable client IP."""
    if not allowlist:
        return True

    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False

    for entry in allowlist:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except (ValueError, TypeError):
            continue
        if addr.version == network.version and addr in network:
            return True

    return False


def two_factor_required(
    org: Org, user: User, *, now: datetime | None = None
) -> bool:
    if not org.require_2fa or user.totp_enabled:
        return False

    grace_until = org.require_2fa_grace_until
    if grace_until is None:
        return True

    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if grace_until.tzinfo is None:
        grace_until = grace_until.replace(tzinfo=timezone.utc)

    return current > grace_until


def _csv_safe_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection."""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def csv_response(rows: list[dict], fieldnames: list[str], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {field: _csv_safe_cell(row.get(field, "")) for field in fieldnames}
        )

    return Response(
        content=buffer.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
