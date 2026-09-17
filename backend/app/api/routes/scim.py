"""P42: SCIM 2.0 (RFC 7643/7644) so a customer's identity provider can add and remove its
people automatically - the moment someone leaves the company, their access here ends.

Scope, deliberately small:
  Users   list (filter ``userName eq``), get, create, replace, patch, delete
  Groups  list, get, patch members  (a group is a workspace ROLE; owner roles never)

Rules
  - Bearer ``ScimToken`` (hashed, per workspace). Nothing else authenticates here.
  - People can only be created or linked on a DNS-VERIFIED domain of the workspace.
  - ``active: false`` or DELETE removes the membership and ends that person's sessions at
    once. The account itself stays (it may belong to other workspaces).
  - Owners can never be deprovisioned, re-roled, or created through SCIM.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.enterprise_sso import SCIM_TOKEN_PREFIX, hash_scim_token
from app.auth.security import hash_password
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.models import Org, OrgMembership, Role, ScimToken, User
from app.rate_limit import enforce_rate_limit
from app.services import account_security, sso_provisioning
from app.services import audit as audit_svc

router = APIRouter(prefix="/scim/v2", tags=["scim"])
logger = structlog.get_logger("scim")

MEDIA = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
MAX_PAGE = 200


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def scim_error_response(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(exc.status),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(body, status_code=exc.status, media_type=MEDIA)


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA)


class ScimContext:
    def __init__(self, session: AsyncSession, org: Org, token: ScimToken, request: Request):
        self.session = session
        self.org = org
        self.token = token
        self.request = request

    @property
    def settings(self):
        return self.request.app.state.settings


async def scim_context(
    request: Request, session: AsyncSession = Depends(get_session)
) -> ScimContext:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    parts = token.strip().split("_", 2)
    if scheme.lower() != "bearer" or len(parts) != 3 or parts[0] != SCIM_TOKEN_PREFIX:
        raise ScimError(401, "Missing or invalid SCIM token")
    await enforce_rate_limit(request, f"scim:{parts[1]}")
    # JUSTIFIED allow_unscoped: the token IS the tenant resolution; looked up by prefix, then
    # its hash is compared in constant time.
    row = (
        await session.execute(
            sa.select(ScimToken)
            .where(ScimToken.prefix == parts[1])
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.revoked_at is not None
        or not secrets.compare_digest(row.token_hash, hash_scim_token(token.strip()))
    ):
        raise ScimError(401, "Missing or invalid SCIM token")
    org = (
        await session.execute(
            sa.select(Org)
            .where(Org.id == row.org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if org is None or not org.is_active:
        raise ScimError(401, "Missing or invalid SCIM token")
    set_org_context(session, org.id)
    row.last_used_at = datetime.now(timezone.utc)
    return ScimContext(session, org, row, request)


# --- helpers -------------------------------------------------------------------------------


def _location(ctx: ScimContext, kind: str, ident: uuid.UUID) -> str:
    base = (ctx.settings.public_base_url or "").rstrip("/")
    return f"{base}/scim/v2/{kind}/{ident}"


def _user_resource(ctx: ScimContext, user: User, *, active: bool) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "userName": user.email,
        "displayName": user.full_name or user.email,
        "name": {"formatted": user.full_name or ""},
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": active,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": user.updated_at.isoformat() if user.updated_at else None,
            "location": _location(ctx, "Users", user.id),
        },
    }


async def _membership(ctx: ScimContext, user_id: uuid.UUID) -> tuple[OrgMembership, Role] | None:
    row = (
        await ctx.session.execute(
            sa.select(OrgMembership, Role)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == ctx.org.id, OrgMembership.user_id == user_id)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def _member_user(ctx: ScimContext, user_id: str) -> tuple[User, OrgMembership, Role]:
    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise ScimError(404, "User not found") from exc
    found = await _membership(ctx, uid)
    user = await ctx.session.get(User, uid) if found else None
    set_org_context(ctx.session, ctx.org.id)
    if found is None or user is None:
        raise ScimError(404, "User not found")
    return user, found[0], found[1]


def _is_owner_role(role: Role) -> bool:
    return "*" in (role.permissions or [])


def _email_from(payload: dict) -> str:
    email = str(payload.get("userName") or "").strip().lower()
    if "@" not in email:
        for item in payload.get("emails") or []:
            if isinstance(item, dict) and "@" in str(item.get("value") or ""):
                email = str(item["value"]).strip().lower()
                if item.get("primary"):
                    break
    if "@" not in email:
        raise ScimError(400, "userName must be an email address", "invalidValue")
    return email


def _name_from(payload: dict) -> str:
    if payload.get("displayName"):
        return str(payload["displayName"])[:255]
    name = payload.get("name") or {}
    if isinstance(name, dict):
        if name.get("formatted"):
            return str(name["formatted"])[:255]
        return " ".join(str(name[k]) for k in ("givenName", "familyName") if name.get(k))[:255]
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


async def _deprovision(ctx: ScimContext, user: User, membership: OrgMembership, role: Role) -> None:
    if _is_owner_role(role):
        raise ScimError(403, "Workspace owners cannot be removed through SCIM", "mutability")
    await ctx.session.delete(membership)
    revoked = await account_security.revoke_sessions(
        ctx.session, ctx.settings, user.id, revoked_by=ctx.token.created_by or user.id
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="scim.user_deprovisioned",
        target_type="user",
        target_id=str(user.id),
        detail={
            "email": user.email,
            "token_prefix": ctx.token.prefix,
            "sessions_ended": len(revoked),
        },
    )
    await ctx.session.commit()
    await account_security.mark_revoked(ctx.settings, revoked)


async def _default_role(ctx: ScimContext) -> Role:
    from app.services.sso_provisioning import _role_for_new_member

    return await _role_for_new_member(ctx.session, ctx.org, [])


async def _role_by_id(ctx: ScimContext, group_id: str) -> Role:
    try:
        rid = uuid.UUID(group_id)
    except ValueError as exc:
        raise ScimError(404, "Group not found") from exc
    role = (await ctx.session.execute(sa.select(Role).where(Role.id == rid))).scalar_one_or_none()
    if role is None or _is_owner_role(role):
        raise ScimError(404, "Group not found")
    return role


# --- discovery ----------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(ctx: ScimContext = Depends(scim_context)) -> JSONResponse:
    return _json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_PAGE},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "primary": True,
                    "description": "SCIM token created in workspace security settings",
                }
            ],
        }
    )


@router.get("/ResourceTypes")
async def resource_types(ctx: ScimContext = Depends(scim_context)) -> JSONResponse:
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": 2,
            "Resources": [
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": USER_SCHEMA,
                },
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "Group",
                    "name": "Group",
                    "endpoint": "/Groups",
                    "schema": GROUP_SCHEMA,
                },
            ],
        }
    )


# --- Users ----------------------------------------------------------------------------------


def _parse_filter(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.strip().split(None, 2)
    if (
        len(parts) != 3
        or parts[0].lower() not in ("username", "emails.value")
        or parts[1].lower() != "eq"
    ):
        raise ScimError(400, "Only 'userName eq \"...\"' filters are supported", "invalidFilter")
    return parts[2].strip().strip('"').lower()


@router.get("/Users")
async def list_users(
    ctx: ScimContext = Depends(scim_context),
    filter: str | None = None,  # noqa: A002 - SCIM query parameter name
    startIndex: int = 1,  # noqa: N803
    count: int = 100,
) -> JSONResponse:
    email = _parse_filter(filter)
    start = max(1, startIndex)
    count = max(0, min(count, MAX_PAGE))
    stmt = (
        sa.select(User)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(OrgMembership.org_id == ctx.org.id)
    )
    if email is not None:
        stmt = stmt.where(sa.func.lower(User.email) == email)
    total = (
        await ctx.session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    users = (
        (await ctx.session.execute(stmt.order_by(User.email).offset(start - 1).limit(count)))
        .scalars()
        .all()
    )
    await ctx.session.commit()
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(users),
            "Resources": [_user_resource(ctx, u, active=True) for u in users],
        }
    )


@router.get("/Users/{user_id}")
async def get_user(user_id: str, ctx: ScimContext = Depends(scim_context)) -> JSONResponse:
    user, _m, _r = await _member_user(ctx, user_id)
    await ctx.session.commit()
    return _json(_user_resource(ctx, user, active=True))


@router.post("/Users")
async def create_user(request: Request, ctx: ScimContext = Depends(scim_context)) -> JSONResponse:
    payload = await _body(request)
    email = _email_from(payload)
    if not _truthy(payload.get("active", True)):
        raise ScimError(400, "Create people as active; deactivate them with PATCH", "invalidValue")
    domain = email.partition("@")[2]
    if not await sso_provisioning.domain_is_trusted(ctx.session, ctx.settings, ctx.org, domain):
        raise ScimError(
            403,
            f"Verify {domain} for this workspace before provisioning its people",
            "invalidValue",
        )
    # JUSTIFIED allow_unscoped: accounts are global; matched by email on a verified domain.
    user = (
        await ctx.session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email)
            .limit(1)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    set_org_context(ctx.session, ctx.org.id)
    if user is not None:
        if await _membership(ctx, user.id) is not None:
            raise ScimError(409, "User already exists in this workspace", "uniqueness")
        if not user.is_active:
            raise ScimError(409, "This account is disabled", "uniqueness")
    else:
        # Random password: provisioned people sign in through SSO or a password reset.
        user = User(
            id=uuid.uuid4(),
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name=_name_from(payload),
            is_active=True,
        )
        ctx.session.add(user)
        await ctx.session.flush()
    role = await _default_role(ctx)
    ctx.session.add(OrgMembership(org_id=ctx.org.id, user_id=user.id, role_id=role.id))
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="scim.user_provisioned",
        target_type="user",
        target_id=str(user.id),
        detail={"email": email, "role": role.name, "token_prefix": ctx.token.prefix},
    )
    await ctx.session.commit()
    await ctx.session.refresh(user)
    return _json(_user_resource(ctx, user, active=True), status=201)


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: str, request: Request, ctx: ScimContext = Depends(scim_context)
) -> JSONResponse:
    payload = await _body(request)
    user, membership, role = await _member_user(ctx, user_id)
    if not _truthy(payload.get("active", True)):
        await _deprovision(ctx, user, membership, role)
        return _json(_user_resource(ctx, user, active=False))
    name = _name_from(payload)
    if name:
        user.full_name = name
    await ctx.session.commit()
    await ctx.session.refresh(user)
    return _json(_user_resource(ctx, user, active=True))


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str, request: Request, ctx: ScimContext = Depends(scim_context)
) -> JSONResponse:
    payload = await _body(request)
    user, membership, role = await _member_user(ctx, user_id)
    active = True
    name = ""
    for op in payload.get("Operations") or []:
        if not isinstance(op, dict) or str(op.get("op", "")).lower() not in ("replace", "add"):
            continue
        path = str(op.get("path") or "").lower()
        value = op.get("value")
        if path == "active":
            active = _truthy(value)
        elif path in ("displayname", "name.formatted"):
            name = str(value or "")[:255]
        elif not path and isinstance(value, dict):
            lowered = {str(k).lower(): v for k, v in value.items()}
            if "active" in lowered:
                active = _truthy(lowered["active"])
            if lowered.get("displayname"):
                name = str(lowered["displayname"])[:255]
    if not active:
        await _deprovision(ctx, user, membership, role)
        return _json(_user_resource(ctx, user, active=False))
    if name:
        user.full_name = name
    await ctx.session.commit()
    await ctx.session.refresh(user)
    return _json(_user_resource(ctx, user, active=True))


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, ctx: ScimContext = Depends(scim_context)) -> Response:
    user, membership, role = await _member_user(ctx, user_id)
    await _deprovision(ctx, user, membership, role)
    return Response(status_code=204)


# --- Groups (= roles) -------------------------------------------------------------------------


async def _group_resource(ctx: ScimContext, role: Role) -> dict:
    members = (
        await ctx.session.execute(
            sa.select(User.id, User.email)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .where(OrgMembership.org_id == ctx.org.id, OrgMembership.role_id == role.id)
            .order_by(User.email)
        )
    ).all()
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(role.id),
        "displayName": role.name,
        "members": [{"value": str(uid), "display": email} for uid, email in members],
        "meta": {"resourceType": "Group", "location": _location(ctx, "Groups", role.id)},
    }


@router.get("/Groups")
async def list_groups(
    ctx: ScimContext = Depends(scim_context),
    filter: str | None = None,  # noqa: A002
) -> JSONResponse:
    roles = (await ctx.session.execute(sa.select(Role).order_by(Role.name))).scalars().all()
    roles = [r for r in roles if not _is_owner_role(r)]
    if filter:
        parts = filter.strip().split(None, 2)
        if len(parts) != 3 or parts[0].lower() != "displayname" or parts[1].lower() != "eq":
            raise ScimError(
                400, "Only 'displayName eq \"...\"' filters are supported", "invalidFilter"
            )
        wanted = parts[2].strip().strip('"')
        roles = [r for r in roles if r.name == wanted]
    resources = [await _group_resource(ctx, r) for r in roles]
    await ctx.session.commit()
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": len(resources),
            "startIndex": 1,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
    )


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, ctx: ScimContext = Depends(scim_context)) -> JSONResponse:
    role = await _role_by_id(ctx, group_id)
    body = await _group_resource(ctx, role)
    await ctx.session.commit()
    return _json(body)


@router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: str, request: Request, ctx: ScimContext = Depends(scim_context)
) -> JSONResponse:
    payload = await _body(request)
    role = await _role_by_id(ctx, group_id)
    default_role = await _default_role(ctx)
    changes: list[dict] = []
    for op in payload.get("Operations") or []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).lower()
        path = str(op.get("path") or "")
        values = op.get("value")
        member_ids: list[str] = []
        if path.startswith("members[") and "eq" in path:
            member_ids = [path.split("eq", 1)[1].strip(' "]')]
        elif isinstance(values, list):
            member_ids = [
                str(v.get("value")) for v in values if isinstance(v, dict) and v.get("value")
            ]
        if kind not in ("add", "remove") or not (path.lower().startswith("members") or not path):
            continue
        for member_id in member_ids:
            user, membership, current = await _member_user(ctx, member_id)
            if _is_owner_role(current):
                raise ScimError(
                    403, "Workspace owners cannot be re-roled through SCIM", "mutability"
                )
            if kind == "add" and membership.role_id != role.id:
                membership.role_id = role.id
                changes.append({"user": user.email, "role": role.name})
            elif kind == "remove" and membership.role_id == role.id and role.id != default_role.id:
                membership.role_id = default_role.id
                changes.append({"user": user.email, "role": default_role.name})
                # Losing a role is losing access: end sessions so old permissions don't linger.
                revoked = await account_security.revoke_sessions(
                    ctx.session, ctx.settings, user.id, revoked_by=ctx.token.created_by or user.id
                )
                await account_security.mark_revoked(ctx.settings, revoked)
    if changes:
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="scim.roles_changed",
            target_type="role",
            target_id=str(role.id),
            detail={"changes": changes, "token_prefix": ctx.token.prefix},
        )
    body = await _group_resource(ctx, role)
    await ctx.session.commit()
    return _json(body)


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ScimError(400, "Body must be JSON", "invalidSyntax") from exc
    if not isinstance(payload, dict):
        raise ScimError(400, "Body must be a JSON object", "invalidSyntax")
    return payload
