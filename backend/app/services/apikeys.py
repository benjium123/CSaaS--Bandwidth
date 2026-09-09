"""API key management (P13 DR-3).

Full key format: ``csk_<prefix>_<secret>``. The database stores the prefix + a SHA-256
hash only - the full key is returned to the caller exactly ONCE, at creation (and, for
the new key, at rotation). Scopes are a SUBSET of the RBAC permission catalogue; the
``*`` wildcard is refused outright - an API key must never carry owner-equivalent access.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import generate_api_key
from app.errors import ValidationFailedError
from app.models import PERMISSIONS, WILDCARD, ApiKey, OrgMembership, Role
from app.services import audit as audit_svc


def _validate_scopes(scopes: list[str]) -> list[str]:
    if not scopes:
        raise ValidationFailedError("At least one scope is required")
    if WILDCARD in scopes:
        raise ValidationFailedError("API keys cannot be granted the '*' wildcard scope")
    unknown = [s for s in scopes if s not in PERMISSIONS]
    if unknown:
        raise ValidationFailedError(f"Unknown permission keys: {', '.join(sorted(unknown))}")
    return scopes


async def create(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
    created_by: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
    #: C2: the SCOPES of the API key that authenticated this call, when the caller was
    #: an API key rather than a human user. A caller with no actor_user_id/created_by
    #: MUST pass this so the subset check below still runs against something.
    actor_key_scopes: list[str] | None = None,
) -> tuple[ApiKey, str]:
    """Returns ``(row, full_key)``. ``full_key`` is shown ONCE - it is never
    recoverable again; only the prefix + hash are persisted."""
    _validate_scopes(scopes)

    effective_user_id = actor_user_id or created_by
    if effective_user_id is not None:
        found = (
            await session.execute(
                sa.select(OrgMembership, Role)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(
                    OrgMembership.org_id == org_id,
                    OrgMembership.user_id == effective_user_id,
                )
            )
        ).first()
        if found is None:
            raise ValidationFailedError(
                "API-key creator is not a member of this organisation"
            )
        _, actor_role = found
        if WILDCARD not in (actor_role.permissions or []):
            effective = set(actor_role.permissions or [])
            exceeding = sorted(set(scopes) - effective)
            if exceeding:
                raise ValidationFailedError(
                    "Scopes exceed the creator's permissions: " + ", ".join(exceeding)
                )
    elif actor_api_key_id is not None:
        # C2: an API-key-authenticated caller has no actor_user_id/created_by at all,
        # so the subset check above was silently SKIPPED entirely - a key could mint a
        # new key with scopes exceeding its own. Gate on the authenticating key's own
        # scopes instead.
        if actor_key_scopes is None:
            raise ValidationFailedError(
                "Cannot resolve the authenticating API key's scopes"
            )
        exceeding = sorted(set(scopes) - set(actor_key_scopes))
        if exceeding:
            raise ValidationFailedError(
                "Scopes exceed the authenticating key's scopes: " + ", ".join(exceeding)
            )
    else:
        # C2: no actor could be resolved at all (neither a user nor an API key) -
        # refuse rather than silently create the key with no scope validation.
        raise ValidationFailedError("Cannot resolve an actor to validate scopes against")

    full_key, prefix, key_hash = generate_api_key()
    row = ApiKey(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name,
        prefix=prefix,
        key_hash=key_hash,
        scopes=list(scopes),
        status="active",
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    audit_svc.record(
        session,
        org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="apikey.created",
        target_type="api_key",
        target_id=str(row.id),
        detail={"name": name, "prefix": prefix, "scopes": list(scopes)},
    )
    await session.commit()
    return row, full_key


async def list_keys(session: AsyncSession, org_id: uuid.UUID) -> list[ApiKey]:
    stmt = sa.select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def revoke(
    session: AsyncSession,
    key: ApiKey,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> ApiKey:
    if key.status == "revoked":
        return key
    key.status = "revoked"
    audit_svc.record(
        session,
        key.org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="apikey.revoked",
        target_type="api_key",
        target_id=str(key.id),
        detail={"name": key.name, "prefix": key.prefix},
    )
    await session.commit()
    return key


async def rotate(
    session: AsyncSession,
    key: ApiKey,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> tuple[ApiKey, str]:
    """Create-new + revoke-old, atomically - one commit for both halves."""
    if key.status != "active":
        raise ValidationFailedError("Only an active key can be rotated")
    full_key, prefix, key_hash = generate_api_key()
    new_row = ApiKey(
        id=uuid.uuid4(),
        org_id=key.org_id,
        name=key.name,
        prefix=prefix,
        key_hash=key_hash,
        scopes=list(key.scopes or []),
        status="active",
        expires_at=key.expires_at,
        created_by=key.created_by,
    )
    session.add(new_row)
    key.status = "revoked"
    await session.flush()
    audit_svc.record(
        session,
        key.org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="apikey.rotated",
        target_type="api_key",
        target_id=str(new_row.id),
        detail={"name": key.name, "old_prefix": key.prefix, "new_prefix": prefix},
    )
    await session.commit()
    return new_row, full_key
